package com.framekeep.inventory;

import android.content.Context;
import androidx.annotation.NonNull;
import androidx.work.*;

public final class SyncWorker extends Worker {
    public SyncWorker(@NonNull Context context,@NonNull WorkerParameters params){super(context,params);}
    static void enqueue(Context context){
        WorkManager.getInstance(context).enqueueUniqueWork("inventory-sync",ExistingWorkPolicy.APPEND_OR_REPLACE,new OneTimeWorkRequest.Builder(SyncWorker.class).build());
    }
    @NonNull @Override public Result doWork(){
        InventoryStore store=new InventoryStore(getApplicationContext());
        try{if(!store.cleanupPending())InventorySync.run(getApplicationContext(),store,null);OcrWorker.notifyRecord(getApplicationContext(),"");return Result.success();}
        catch(Exception unavailable){return Result.success();}finally{store.close();}
    }
}
