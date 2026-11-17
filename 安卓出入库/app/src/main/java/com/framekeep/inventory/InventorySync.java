package com.framekeep.inventory;

import android.content.*;
import org.json.*;
import java.io.IOException;

/** 网络同步独立于拍摄保存线程；只有 SKU 已确认的记录进入原有桌面协议。 */
final class InventorySync {
    interface Progress {void count(int count) throws Exception;}
    static synchronized JSONObject run(Context context,InventoryStore store,Progress progress) throws Exception {
        SharedPreferences prefs=context.getSharedPreferences("inventory",0);
        String url=Net.base(prefs.getString("url","")),token=prefs.getString("token","");if(token.isEmpty())throw new IOException("请填写工作台连接令牌");
        JSONObject health=Net.json("GET",url+"/v1/health",token,null,10000);int count=0,offset=0;
        if(health.optInt("protocol")!=1)throw new IOException("工作台通信协议不匹配");
        String target=health.optString("workspace",url);boolean replay=!target.equals(prefs.getString("lastSyncTarget",""));
        while(true){
            if(store.cleanupPending())throw new IOException("本机正在清理，同步已停止");
            JSONArray records=store.syncList(replay?offset:0,1,!replay);if(records.length()==0)break;JSONObject r=records.getJSONObject(0);
            JSONObject ack=Net.json("POST",url+"/v1/records",token,store.payload(r),30000);
            if(!ack.optString("id").equals(r.getString("id"))||!ack.optString("product_sha").equals(r.getString("product_sha"))||!ack.optString("label_sha").equals(r.getString("label_sha")))throw new IOException("工作台回执不一致，记录保持待同步");
            if(ack.has("revision")&&ack.optInt("revision",0)!=r.optInt("ocr_revision",0))throw new IOException("工作台尚未确认最新款号，请更新电脑端后重试");
            store.acknowledge(r.getString("id"),r.optInt("ocr_revision",0));count++;offset++;if(progress!=null)progress.count(count);
        }
        prefs.edit().putString("lastSyncTarget",target).commit();
        return new JSONObject().put("count",count).put("rules",health.optJSONArray("rules")).put("theme",health.optString("theme","dark"));
    }
}
