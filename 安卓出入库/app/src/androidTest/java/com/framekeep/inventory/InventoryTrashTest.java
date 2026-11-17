package com.framekeep.inventory;

import android.content.*;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.graphics.Bitmap;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import java.io.*;
import java.nio.file.Files;
import java.util.*;
import java.util.zip.ZipFile;
import static org.junit.Assert.*;

/** 在独立验证包中测试真实 SQLite、文件和导出，不接触用户账本。 */
@RunWith(AndroidJUnit4.class)
public class InventoryTrashTest {
    Context context;InventoryStore store;String database,folder;
    @Before public void setup(){
        context=InstrumentationRegistry.getInstrumentation().getTargetContext();
        if(!context.getSharedPreferences("inventory",0).contains("device"))context.getSharedPreferences("inventory",0).edit().putString("device",UUID.randomUUID().toString()).commit();
        String key=UUID.randomUUID().toString();database="trash-test-"+key+".db";folder="trash-test-"+key;
        store=new InventoryStore(context,database,folder);
    }
    @After public void cleanup() throws Exception {
        store.close();context.deleteDatabase(database);File dir=new File(context.getFilesDir(),folder);
        File[] files=dir.listFiles();if(files!=null)for(File file:files)assertTrue(file.delete());if(dir.exists())assertTrue(dir.delete());
    }
    JSONObject save(String sku,String mode) throws Exception {
        String id=UUID.randomUUID().toString();File product=new File(store.images,"p.jpg"),label=new File(store.images,"l.jpg");
        Bitmap b=Bitmap.createBitmap(4,4,Bitmap.Config.ARGB_8888);for(File file:new File[]{product,label})try(OutputStream out=new FileOutputStream(file)){b.compress(Bitmap.CompressFormat.JPEG,90,out);}b.recycle();
        return store.save(id,mode,sku,product,label,"RAW", "offline","2026-11-05T10:00:00+08:00");
    }
    void deletedAt(String id,long time){ContentValues values=new ContentValues();values.put("deleted_at",time);store.getWritableDatabase().update("records",values,"id=?",new String[]{id});}
    @Test public void deleteRestorePreservesOriginalAndImagesAcrossRestart() throws Exception {
        JSONObject original=save("LC-KEEP","in");String id=original.getString("id");store.acknowledge(id);
        byte[] product=Files.readAllBytes(store.image(id,"product").toPath()),label=Files.readAllBytes(store.image(id,"label").toPath());
        store.trash(id);assertEquals(0,store.list("","",0,60,false).length());assertEquals(0,store.summary().getInt("total"));assertEquals(1,store.trashList("","",0,60).length());
        store.close();store=new InventoryStore(context,database,folder);assertEquals(1,store.trashList("","",0,60).length());store.restore(id);
        JSONObject restored=store.list("","",0,60,false).getJSONObject(0);
        for(String key:new String[]{"id","device","mode","sku","at","raw","source","product_sha","label_sha"})assertEquals(original.get(key),restored.get(key));
        assertEquals(1,restored.getInt("synced"));assertEquals(0,store.summary().getInt("pending"));assertEquals(0,store.trashList("","",0,60).length());
        assertArrayEquals(product,Files.readAllBytes(store.image(id,"product").toPath()));assertArrayEquals(label,Files.readAllBytes(store.image(id,"label").toPath()));
    }
    @Test public void expiredCannotRestoreAndPurgesOnlyExpiredImages() throws Exception {
        String expired=save("LC-OLD","in").getString("id"),kept=save("LC-NEW","out").getString("id"),active=save("LC-ACTIVE","in").getString("id");
        deletedAt(expired,System.currentTimeMillis()-InventoryStore.TRASH_RETENTION_MS);
        deletedAt(kept,System.currentTimeMillis()-InventoryStore.TRASH_RETENTION_MS+60000);
        try{store.restore(expired);fail("30 天边界应拒绝还原");}catch(IOException expected){}
        store.purgeExpired();store.purgeExpired();assertEquals(1,store.trashList("","",0,60).length());assertEquals(1,store.list("","",0,60,false).length());
        assertFalse(new File(store.images,expired+"-product.jpg").exists());assertFalse(new File(store.images,expired+"-label.jpg").exists());
        assertTrue(store.image(kept,"product").isFile());assertTrue(store.image(active,"label").isFile());store.restore(kept);assertEquals(2,store.summary().getInt("total"));
    }
    @Test public void deletedExcludedFromPendingExportAndPayload() throws Exception {
        JSONObject deleted=save("LC-DELETED","in");String id=deleted.getString("id");save("LC-LIVE","out");store.trash(id);
        assertEquals(1,store.summary().getInt("pending"));assertEquals(0,store.summary().getInt("in"));assertEquals(1,store.summary().getInt("out"));assertEquals(1,store.list("","",0,500,true).length());
        try{store.payload(deleted);fail("删除后不应发送旧列表快照");}catch(IOException expected){}
        JSONObject payload=store.payload(store.list("","",0,1,false).getJSONObject(0));assertFalse(payload.has("deleted_at"));
        File export=new File(store.images,"test.xlsx");Workbook.write(export,store,"","");
        try(ZipFile zip=new ZipFile(export);InputStream input=zip.getInputStream(zip.getEntry("xl/worksheets/sheet1.xml"));ByteArrayOutputStream bytes=new ByteArrayOutputStream()){byte[] buffer=new byte[4096];int count;while((count=input.read(buffer))!=-1)bytes.write(buffer,0,count);String xml=new String(bytes.toByteArray(),java.nio.charset.StandardCharsets.UTF_8);assertTrue(xml.contains("LC-LIVE"));assertFalse(xml.contains("LC-DELETED"));assertNotNull(zip.getEntry("xl/media/image2.jpg"));assertNull(zip.getEntry("xl/media/image3.jpg"));}
        store.restore(id);assertEquals(2,store.summary().getInt("pending"));
    }
    @Test public void duplicateActionsAndCleanupCannotChangeWrongRows() throws Exception {
        String id=save("LC-ONCE","in").getString("id");store.trash(id);
        try{store.trash(id);fail("重复删除不应重置期限");}catch(IOException expected){}
        store.restore(id);try{store.restore(id);fail("不能重复还原");}catch(IOException expected){}
        store.getWritableDatabase().execSQL("INSERT INTO maintenance(key,value) VALUES('clear_pending',1)");
        try{store.trash(id);fail("全量清理期间不应修改");}catch(IOException expected){}
        assertEquals(1,store.summary().getInt("total"));
    }
    @Test public void filteredTrashPaginationHasNoDuplicates() throws Exception {
        for(int i=0;i<8;i++){JSONObject r=save("LC-PAGE-"+i,i%2==0?"in":"out");store.trash(r.getString("id"));}
        Set<String> ids=new HashSet<>();for(int offset=0;offset<4;offset+=2){JSONArray rows=store.trashList("in","page",offset,2);assertEquals(2,rows.length());for(int i=0;i<rows.length();i++)assertTrue(ids.add(rows.getJSONObject(i).getString("id")));}
        assertEquals(0,store.trashList("in","page",4,2).length());assertEquals(0,store.trashList("","missing",0,60).length());
    }
    @Test public void upgradesVersionOneWithoutLosingRecordsOrImages() throws Exception {
        JSONObject original=save("LC-MIGRATE","in");String id=original.getString("id");store.acknowledge(id);store.close();
        try(SQLiteDatabase db=context.openOrCreateDatabase(database,0,null)){
            db.execSQL("DROP INDEX records_deleted");db.execSQL("ALTER TABLE records RENAME TO old_records");
            db.execSQL("CREATE TABLE records AS SELECT id,device,mode,sku,at,raw,source,product_sha,label_sha,synced FROM old_records");db.execSQL("DROP TABLE old_records");db.execSQL("CREATE INDEX records_time ON records(at DESC,id)");db.execSQL("DROP TABLE sync_changes");db.setVersion(1);
        }
        store=new InventoryStore(context,database,folder);assertEquals(4,store.getReadableDatabase().getVersion());assertEquals(1,store.summary().getInt("total"));assertEquals(1,store.list("","",0,60,false).getJSONObject(0).getInt("synced"));assertTrue(store.image(id,"product").isFile());
        store.trash(id);store.restore(id);assertEquals(original.getString("at"),store.list("","",0,60,false).getJSONObject(0).getString("at"));
    }
}
