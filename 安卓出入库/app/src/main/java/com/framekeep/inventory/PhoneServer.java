package com.framekeep.inventory;
import fi.iki.elonen.NanoHTTPD;
import org.json.*;
import java.io.*;
import java.security.MessageDigest;
import java.nio.charset.StandardCharsets;
final class PhoneServer extends NanoHTTPD {
    final InventoryStore store;final String token;final Runnable touched;final java.util.function.Consumer<String> acked;
    PhoneServer(InventoryStore s,String t) throws IOException {this(s,t,null,8792,()->{});}
    PhoneServer(InventoryStore s,String t,String host,int port,Runnable touch) throws IOException {this(s,t,host,port,touch,id->{});}
    PhoneServer(InventoryStore s,String t,String host,int port,Runnable touch,java.util.function.Consumer<String> onAck) throws IOException {super(host,port);store=s;token=t;touched=touch;acked=onAck;start(SOCKET_READ_TIMEOUT,false);}
    Response json(JSONObject body,Response.Status status){Response r=newFixedLengthResponse(status,"application/json; charset=utf-8",body.toString());r.addHeader("Cache-Control","no-store");r.addHeader("X-Content-Type-Options","nosniff");return r;}
    @Override public Response serve(IHTTPSession session) {
        try {
            if(!MessageDigest.isEqual(("Bearer "+token).getBytes(StandardCharsets.UTF_8),session.getHeaders().getOrDefault("authorization","").getBytes(StandardCharsets.UTF_8)))return json(new JSONObject().put("error","手机连接令牌无效"),Response.Status.UNAUTHORIZED);
            touched.run();
            String path=session.getUri();
            if(session.getMethod()==Method.GET&&path.equals("/v1/health"))return json(new JSONObject().put("device",store.device).put("protocol",1).put("capabilities",new JSONArray().put("delta")).put("model",android.os.Build.MODEL).put("summary",store.summary()),Response.Status.OK);
            if(session.getMethod()==Method.GET&&path.equals("/v1/changes"))return json(store.changes(Long.parseLong(session.getParms().getOrDefault("after","0")),100),Response.Status.OK);
            if(session.getMethod()==Method.GET&&path.equals("/v1/record")){
                JSONObject row;try{row=store.record(session.getParms().getOrDefault("id",""));if(!row.optString("ocr_status").equals("ready"))throw new IOException();}catch(IOException missing){return json(new JSONObject().put("error","记录已删除或尚未确认"),Response.Status.NOT_FOUND);}
                return json(store.payload(row),Response.Status.OK);
            }
            if(session.getMethod()==Method.GET&&path.equals("/v1/records")) {
                int offset=Integer.parseInt(session.getParms().getOrDefault("offset","0"));JSONArray rows=store.syncList(offset,1,false),a=new JSONArray();for(int i=0;i<rows.length();i++)a.put(store.payload(rows.getJSONObject(i)));
                return json(new JSONObject().put("records",a).put("next",offset+a.length()).put("done",a.length()==0),Response.Status.OK);
            }
            if(session.getMethod()==Method.POST&&path.equals("/v1/ack")){
                int len=Integer.parseInt(session.getHeaders().getOrDefault("content-length","0"));if(len<=0||len>4096)throw new IOException("确认请求无效");
                java.util.Map<String,String> data=new java.util.HashMap<>();session.parseBody(data);JSONObject body=new JSONObject(data.getOrDefault("postData","{}"));
                String id=body.getString("id");JSONArray rows=store.list("","",0,500,false);
                try(android.database.Cursor c=store.getReadableDatabase().rawQuery("SELECT product_sha,label_sha FROM records WHERE id=?",new String[]{id})){if(!c.moveToFirst()||!c.getString(0).equals(body.getString("product_sha"))||!c.getString(1).equals(body.getString("label_sha")))throw new IOException("回执校验失败");}
                if(!store.acknowledge(id,body.optInt("revision",0)))return json(new JSONObject().put("error","款号已变化，请重新同步最新记录"),Response.Status.CONFLICT);acked.accept(id);return json(new JSONObject().put("ok",true),Response.Status.OK);
            }
            return json(new JSONObject().put("error","接口不存在"),Response.Status.NOT_FOUND);
        } catch(Exception e){try{return json(new JSONObject().put("error","手机请求处理失败"),Response.Status.BAD_REQUEST);}catch(JSONException impossible){return newFixedLengthResponse(Response.Status.INTERNAL_ERROR,"text/plain","error");}}
    }
}
