package com.framekeep.inventory;

import android.content.*;
import android.database.sqlite.SQLiteDatabase;
import android.graphics.*;
import android.os.Bundle;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import java.io.*;
import java.nio.file.Files;
import java.util.*;
import static org.junit.Assert.*;

@RunWith(AndroidJUnit4.class)
public class InventoryOcrTest {
    Context context;InventoryStore store;String database,folder;
    @Before public void setup(){
        context=InstrumentationRegistry.getInstrumentation().getTargetContext();
        if(!context.getSharedPreferences("inventory",0).contains("device"))context.getSharedPreferences("inventory",0).edit().putString("device",UUID.randomUUID().toString()).commit();
        String key=UUID.randomUUID().toString();database="ocr-test-"+key+".db";folder="ocr-test-"+key;store=new InventoryStore(context,database,folder);
    }
    @After public void cleanup(){store.close();context.deleteDatabase(database);File dir=new File(context.getFilesDir(),folder);File[] files=dir.listFiles();if(files!=null)for(File file:files)assertTrue(file.delete());assertTrue(dir.delete());}
    JSONObject capture() throws Exception {
        return capture(OcrModels.DEFAULT);
    }
    JSONObject capture(String model) throws Exception {
        File product=new File(store.images,"p.jpg"),label=new File(store.images,"l.jpg");Bitmap bitmap=Bitmap.createBitmap(10,10,Bitmap.Config.ARGB_8888);
        for(File file:new File[]{product,label})try(OutputStream out=new FileOutputStream(file)){bitmap.compress(Bitmap.CompressFormat.JPEG,90,out);}bitmap.recycle();
        return store.saveCaptured(UUID.randomUUID().toString(),"in",product,label,"2026-11-06T10:00:00+08:00",model);
    }
    JSONObject recognized(String text) throws Exception {return SkuRecognizer.analyze(text,"LC","test");}
    @Test public void captureImmediatelyCountsWithoutSkuAndQueueSurvivesRestart() throws Exception {
        JSONObject row=capture();assertEquals("pending",row.getString("ocr_status"));assertEquals("",row.getString("sku"));assertEquals(1,store.summary().getInt("in"));assertEquals(0,store.summary().getInt("pending"));assertEquals(0,store.syncList(0,60,false).length());
        store.close();store=new InventoryStore(context,database,folder);assertEquals(1,store.queued().length());assertTrue(store.image(row.getString("id"),"product").isFile());
    }
    @Test public void uniqueMatchCompletesSameRecordAndBecomesSyncable() throws Exception {
        JSONObject row=capture();String id=row.getString("id"),sha=row.getString("product_sha");assertTrue(store.finishRecognition(id,0,recognized("LC7877529 - P105")));
        JSONObject result=store.record(id);assertEquals("ready",result.getString("ocr_status"));assertEquals("LC7877529-P105",result.getString("sku"));assertEquals(sha,result.getString("product_sha"));assertEquals(row.getString("at"),result.getString("at"));assertEquals(1,store.syncList(0,60,true).length());assertEquals(1,store.summary().getInt("in"));assertFalse(store.payload(result).has("ocr_status"));
    }
    @Test public void queuedModelSnapshotSurvivesSettingsChangeRestartAndDuplicateSave() throws Exception {
        JSONObject row=capture("paddle-medium");String id=row.getString("id");android.content.SharedPreferences prefs=context.getSharedPreferences("inventory",0);String previous=prefs.getString("ocrModel",null);
        try{
            prefs.edit().putString("ocrModel","mlkit-latin").commit();store.close();store=new InventoryStore(context,database,folder);
            assertEquals("paddle-medium",store.record(id).getJSONObject("recognition").getString("requestedModel"));
            store.saveCaptured(id,"in",new File(store.images,"missing1"),new File(store.images,"missing2"),row.getString("at"),"paddle-small");
            assertEquals("paddle-medium",store.record(id).getJSONObject("recognition").getString("requestedModel"));assertEquals(1,store.summary().getInt("total"));
        }finally{android.content.SharedPreferences.Editor edit=prefs.edit();if(previous==null)edit.remove("ocrModel");else edit.putString("ocrModel",previous);edit.commit();}
    }
    @Test public void syncJournalPublishesOnlyConfirmedRecordsAndDoesNotSkipLateOcr() throws Exception {
        String first=capture().getString("id"),second=capture().getString("id");assertEquals(0,store.changes(0,100).getJSONArray("records").length());
        store.finishRecognition(second,0,recognized("LC2222"));JSONObject batch=store.changes(0,100);assertEquals(second,batch.getJSONArray("records").getJSONObject(0).getString("id"));long cursor=batch.getLong("next");
        store.acknowledge(second);assertEquals(0,store.changes(cursor,100).getJSONArray("records").length());store.finishRecognition(first,0,recognized("LC1111"));batch=store.changes(cursor,100);assertEquals(first,batch.getJSONArray("records").getJSONObject(0).getString("id"));assertTrue(batch.getLong("next")>cursor);
    }
    @Test public void deletedChangeAdvancesCursorAndRestorePublishesAgain() throws Exception {
        String id=capture().getString("id");store.finishRecognition(id,0,recognized("LC1234"));store.trash(id);JSONObject empty=store.changes(0,100);assertEquals(0,empty.getJSONArray("records").length());assertTrue(empty.getLong("next")>0);assertTrue(empty.getBoolean("done"));
        store.restore(id);JSONObject restored=store.changes(empty.getLong("next"),100);assertEquals(id,restored.getJSONArray("records").getJSONObject(0).getString("id"));
        long cursor=restored.getLong("next");store.getWritableDatabase().delete("records",null,null);String fresh=capture().getString("id");store.finishRecognition(fresh,0,recognized("LC5678"));JSONObject changes=store.changes(cursor,100);assertEquals(fresh,changes.getJSONArray("records").getJSONObject(0).getString("id"));assertTrue(changes.getLong("next")>cursor);
    }
    @Test public void retryUsesNewChoiceAndInvalidModelCannotModifyLedger() throws Exception {
        JSONObject row=capture("paddle-tiny");String id=row.getString("id");store.finishRecognition(id,0,recognized("AB1234"));JSONObject retry=store.retryRecord(id,0,"mlkit-chinese");
        assertEquals("mlkit-chinese",retry.getJSONObject("recognition").getString("requestedModel"));assertEquals(1,retry.getInt("ocr_revision"));assertFalse(store.finishRecognition(id,0,recognized("LC9999")));
        try{store.retryRecord(id,1,"unknown");fail();}catch(IllegalArgumentException expected){}assertEquals("mlkit-chinese",store.record(id).getJSONObject("recognition").getString("requestedModel"));assertEquals(1,store.summary().getInt("in"));
    }
    @Test public void mismatchNeedsReviewAndManualConfirmDoesNotDuplicate() throws Exception {
        JSONObject row=capture();String id=row.getString("id");assertTrue(store.finishRecognition(id,0,recognized("AB1234-P10")));assertEquals("review",store.record(id).getString("ocr_status"));assertEquals(0,store.syncList(0,60,false).length());
        JSONObject confirmed=store.confirmRecord(id,0," lc1234-p10 ");assertEquals("LC1234-P10",confirmed.getString("sku"));assertEquals("ready",confirmed.getString("ocr_status"));assertEquals(1,store.summary().getInt("total"));assertFalse(store.finishRecognition(id,0,recognized("LC9999")));
        try{store.confirmRecord(id,0,"LC0000");fail();}catch(IOException expected){}
    }
    @Test public void multipleMatchesAndUncertainSuffixAreNeverAutoSaved() throws Exception {
        for(String text:new String[]{"LC1234\nLC5678","LC7877529 P105",""}){JSONObject row=capture();store.finishRecognition(row.getString("id"),0,recognized(text));assertEquals("review",store.record(row.getString("id")).getString("ocr_status"));}
        assertEquals(3,store.summary().getInt("in"));assertEquals(0,store.syncList(0,60,false).length());
    }
    @Test public void deletionAndRestoreInvalidateLateOcrButKeepQueue() throws Exception {
        JSONObject row=capture();String id=row.getString("id");store.trash(id);assertFalse(store.finishRecognition(id,0,recognized("LC1234")));store.restore(id);JSONObject restored=store.record(id);assertEquals("pending",restored.getString("ocr_status"));assertEquals(1,restored.getInt("ocr_revision"));assertFalse(store.finishRecognition(id,0,recognized("LC1234")));assertTrue(store.finishRecognition(id,1,recognized("LC5678")));
    }
    @Test public void retryAndManualConfirmProtectFromStaleResults() throws Exception {
        String id=capture().getString("id");store.finishRecognition(id,0,recognized("AB1234"));JSONObject retry=store.retryRecord(id,0);assertEquals(1,retry.getInt("ocr_revision"));assertFalse(store.finishRecognition(id,0,recognized("LC1234")));store.finishRecognition(id,1,recognized("AB5678"));
        try{store.confirmRecord(id,0,"LC1111");fail();}catch(IOException expected){}store.confirmRecord(id,1,"LC5678");assertEquals("LC5678",store.record(id).getString("sku"));
    }
    @Test public void clearCannotResurrectAndSavedDraftRecoveryIsIdempotent() throws Exception {
        JSONObject row=capture();String id=row.getString("id");JSONObject same=store.saveCaptured(id,"in",new File(store.images,"missing1"),new File(store.images,"missing2"),row.getString("at"));assertEquals(id,same.getString("id"));assertEquals(1,store.summary().getInt("in"));
        store.getWritableDatabase().delete("records",null,null);assertFalse(store.finishRecognition(id,0,recognized("LC1234")));assertEquals(0,store.summary().getInt("total"));
    }
    @Test public void upgradesVersionTwoPreservingTrashAndSyncedState() throws Exception {
        JSONObject active=capture(),deleted=capture();store.trash(deleted.getString("id"));store.close();
        try(SQLiteDatabase db=context.openOrCreateDatabase(database,0,null)){
            db.execSQL("ALTER TABLE records RENAME TO old_records");db.execSQL("CREATE TABLE records AS SELECT id,device,mode,sku,at,raw,source,product_sha,label_sha,synced,deleted_at FROM old_records");db.execSQL("UPDATE records SET sku='LC-OLD',synced=1");db.execSQL("DROP TABLE old_records");db.execSQL("CREATE INDEX records_time ON records(at DESC,id)");db.execSQL("CREATE INDEX records_deleted ON records(deleted_at)");db.execSQL("DROP TABLE sync_changes");db.setVersion(2);
        }
        store=new InventoryStore(context,database,folder);assertEquals(4,store.getReadableDatabase().getVersion());assertEquals("ready",store.record(active.getString("id")).getString("ocr_status"));assertEquals(1,store.record(active.getString("id")).getInt("synced"));assertEquals(1,store.trashList("","",0,60).length());assertEquals(0,store.queued().length());
    }
    @Test public void offlineModelsRecognizeActualLabelAsWholeSku() throws Exception {
        String fixture=InstrumentationRegistry.getArguments().getString("fixture");Assume.assumeNotNull(fixture);File image=new File(fixture);assertTrue(image.isFile());JSONObject benchmark=new JSONObject();SkuRecognizer recognizer=new SkuRecognizer(context);
        for(boolean chinese:new boolean[]{false,true}){long start=System.nanoTime();JSONObject result=recognizer.offline(image,"LC",chinese);result.put("milliseconds",(System.nanoTime()-start)/1_000_000);benchmark.put(chinese?"chinese":"latin",result);}
        Files.write(new File(context.getFilesDir(),"ocr-model-benchmark.json").toPath(),benchmark.toString(2).getBytes(java.nio.charset.StandardCharsets.UTF_8));Bundle status=new Bundle();status.putString("ocr_benchmark",benchmark.toString());InstrumentationRegistry.getInstrumentation().sendStatus(0,status);
        assertEquals("LC7877529-P105",benchmark.getJSONObject("chinese").getString("sku"));assertTrue(benchmark.getJSONObject("chinese").getBoolean("automatic"));
    }
}
