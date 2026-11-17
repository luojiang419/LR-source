package com.framekeep.inventory;

import android.content.*;
import androidx.annotation.NonNull;
import androidx.work.*;
import org.json.*;

public final class OcrWorker extends Worker {
    static final String UPDATE="com.framekeep.inventory.OCR_UPDATED";
    public OcrWorker(@NonNull Context context,@NonNull WorkerParameters params){super(context,params);}
    static void notifyRecord(Context context,String id){context.sendBroadcast(new Intent(UPDATE).setPackage(context.getPackageName()).putExtra("id",id));}
    @NonNull @Override public Result doWork(){
        Context context=getApplicationContext();String id=getInputData().getString("id");int revision=getInputData().getInt("revision",-1);
        InventoryStore store=new InventoryStore(context);
        try{
            JSONObject row;try{row=store.record(id);}catch(Exception missing){return Result.success();}
            if(store.cleanupPending()||!row.optString("ocr_status").equals("pending")||row.optInt("ocr_revision")!=revision)return Result.success();
            JSONObject recognized;
            try{
                SharedPreferences prefs=context.getSharedPreferences("inventory",0);boolean cloud=prefs.getBoolean("cloud",true);String key="";
                try{if(cloud)key=SecretVault.ocrKey(context);}catch(Exception unavailable){cloud=false;}
                String model=row.getJSONObject("recognition").optString("requestedModel",prefs.getString("ocrModel",OcrModels.DEFAULT));
                recognized=new SkuRecognizer(context).recognize(store.image(id,"label"),prefs.getString("rules","LC"),cloud,key,OcrModels.require(model));
            }catch(Exception failed){recognized=new JSONObject().put("automatic",false).put("candidates",new JSONArray()).put("preferred",new JSONArray()).put("warning","识别未完成，请核对款号");}
            if(isStopped())return Result.retry();
            if(store.finishRecognition(id,revision,recognized)){
                notifyRecord(context,id);SharedPreferences prefs=context.getSharedPreferences("inventory",0);
                if(recognized.optBoolean("automatic")&&prefs.getBoolean("autoSync",true)&&!prefs.getString("url","").isEmpty())SyncWorker.enqueue(context);
            }
            return Result.success();
        }catch(Exception unavailable){return Result.retry();}finally{store.close();}
    }
}
