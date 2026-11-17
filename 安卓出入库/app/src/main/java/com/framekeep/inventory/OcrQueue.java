package com.framekeep.inventory;

import android.content.Context;
import androidx.work.*;
import org.json.*;

final class OcrQueue {
    static void enqueue(Context context,JSONObject row){
        Data data=new Data.Builder().putString("id",row.optString("id")).putInt("revision",row.optInt("ocr_revision")).build();
        OneTimeWorkRequest work=new OneTimeWorkRequest.Builder(OcrWorker.class).setInputData(data).addTag("inventory-ocr").build();
        WorkManager.getInstance(context).beginUniqueWork("inventory-ocr",ExistingWorkPolicy.APPEND_OR_REPLACE,work).enqueue();
    }
    static void recover(Context context,InventoryStore store) throws Exception {
        if(store.cleanupPending())return;JSONArray rows=store.queued();for(int i=0;i<rows.length();i++)enqueue(context,rows.getJSONObject(i));
    }
}
