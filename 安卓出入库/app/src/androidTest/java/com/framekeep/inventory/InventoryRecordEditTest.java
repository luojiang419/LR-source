package com.framekeep.inventory;

import android.content.Context;
import android.graphics.Bitmap;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import java.io.*;
import java.util.UUID;
import static org.junit.Assert.*;

/** 使用独立 SQLite 和图片验证款号更正，不触碰正式账本。 */
@RunWith(AndroidJUnit4.class)
public class InventoryRecordEditTest {
    Context context;InventoryStore store;String database,folder;
    @Before public void setup(){
        context=InstrumentationRegistry.getInstrumentation().getTargetContext();
        if(!context.getSharedPreferences("inventory",0).contains("device"))context.getSharedPreferences("inventory",0).edit().putString("device",UUID.randomUUID().toString()).commit();
        String key=UUID.randomUUID().toString();database="edit-test-"+key+".db";folder="edit-test-"+key;store=new InventoryStore(context,database,folder);
    }
    @After public void cleanup(){store.close();context.deleteDatabase(database);File dir=new File(context.getFilesDir(),folder);File[] files=dir.listFiles();if(files!=null)for(File file:files)assertTrue(file.delete());assertTrue(dir.delete());}
    JSONObject capture() throws Exception {
        File product=new File(store.images,"p.jpg"),label=new File(store.images,"l.jpg");Bitmap bitmap=Bitmap.createBitmap(10,10,Bitmap.Config.ARGB_8888);
        for(File file:new File[]{product,label})try(OutputStream out=new FileOutputStream(file)){bitmap.compress(Bitmap.CompressFormat.JPEG,90,out);}bitmap.recycle();
        return store.saveCaptured(UUID.randomUUID().toString(),"in",product,label,"2026-11-09T10:00:00+08:00");
    }
    JSONObject ready() throws Exception {JSONObject r=capture();store.finishRecognition(r.getString("id"),0,SkuRecognizer.analyze("LC1234","LC","test"));return store.record(r.getString("id"));}
    @Test public void editingSyncedRecordPreservesIdentityAndCreatesDelta() throws Exception {
        JSONObject before=ready();String id=before.getString("id");store.acknowledge(id);long cursor=store.changes(0,100).getLong("next");
        JSONObject edited=store.updateRecordSku(id,0," lc5678-p10 ");assertEquals("LC5678-P10",edited.getString("sku"));assertEquals(1,edited.getInt("ocr_revision"));assertEquals(0,edited.getInt("synced"));
        for(String key:new String[]{"id","at","mode","device","raw","product_sha","label_sha"})assertEquals(before.getString(key),edited.getString(key));
        assertEquals(before.getString("product_sha"),InventoryStore.sha(store.image(id,"product")));assertEquals(before.getString("label_sha"),InventoryStore.sha(store.image(id,"label")));
        assertEquals(1,store.summary().getInt("in"));assertEquals(1,store.summary().getInt("pending"));assertEquals(1,store.changes(cursor,100).getJSONArray("records").length());assertEquals(1,store.payload(edited).getInt("revision"));
        store.close();store=new InventoryStore(context,database,folder);assertEquals("LC5678-P10",store.record(id).getString("sku"));assertEquals(1,store.list("","LC5678",0,60,false).length());assertEquals(0,store.list("","LC1234",0,60,false).length());
    }
    @Test public void manualPendingSaveInvalidatesLateRecognition() throws Exception {
        String id=capture().getString("id");store.updateRecordSku(id,0,"LC-MANUAL");assertEquals(0,store.queued().length());assertFalse(store.finishRecognition(id,0,SkuRecognizer.analyze("LC9999","LC","test")));assertEquals("LC-MANUAL",store.record(id).getString("sku"));assertEquals(1,store.summary().getInt("total"));
    }
    @Test public void invalidOrStaleEditNeverChangesStoredSku() throws Exception {
        String id=ready().getString("id");for(String value:new String[]{""," ","中文","=LC123","LC 123"}){try{store.updateRecordSku(id,0,value);fail();}catch(IllegalArgumentException expected){}}
        store.updateRecordSku(id,0,"LC-NEW");try{store.updateRecordSku(id,0,"LC-STALE");fail();}catch(IOException expected){}assertEquals("LC-NEW",store.record(id).getString("sku"));
        store.trash(id);try{store.updateRecordSku(id,1,"LC-DELETED");fail();}catch(IOException expected){}assertEquals(1,store.trashList("","LC-NEW",0,60).length());
    }
    @Test public void unchangedSaveKeepsSyncAndRevision() throws Exception {
        String id=ready().getString("id");store.acknowledge(id);long cursor=store.changes(0,100).getLong("next");JSONObject same=store.updateRecordSku(id,0," lc1234 ");assertEquals(1,same.getInt("synced"));assertEquals(0,same.getInt("ocr_revision"));assertEquals(0,store.changes(cursor,100).getJSONArray("records").length());
    }
    @Test public void staleSyncReceiptCannotAcknowledgeNewEdit() throws Exception {
        String id=ready().getString("id");store.updateRecordSku(id,0,"LC-NEW");assertFalse(store.acknowledge(id,0));assertEquals(0,store.record(id).getInt("synced"));assertTrue(store.acknowledge(id,1));assertEquals(1,store.record(id).getInt("synced"));
    }
}
