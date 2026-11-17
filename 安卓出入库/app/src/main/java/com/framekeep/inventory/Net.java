package com.framekeep.inventory;
import org.json.*;
import java.net.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
final class Net {
    static String base(String text) throws Exception {
        String u=text.trim().replaceAll("/+$","");URI v=new URI(u);
        if(!("https".equals(v.getScheme())||"http".equals(v.getScheme()))||v.getHost()==null||v.getUserInfo()!=null||v.getQuery()!=null||v.getFragment()!=null)throw new IOException("请输入完整 http(s) 地址，可包含反代路径");return u;
    }
    static JSONObject json(String method,String url,String token,JSONObject body,int timeout) throws Exception {
        HttpURLConnection c=(HttpURLConnection)new URL(url).openConnection();c.setRequestMethod(method);c.setConnectTimeout(7000);c.setReadTimeout(timeout);c.setInstanceFollowRedirects(false);c.setRequestProperty("Accept","application/json");if(!token.isEmpty())c.setRequestProperty("Authorization","Bearer "+token);
        try {
            if(body!=null){c.setDoOutput(true);c.setRequestProperty("Content-Type","application/json; charset=utf-8");byte[] bytes=body.toString().getBytes(StandardCharsets.UTF_8);c.setFixedLengthStreamingMode(bytes.length);try(OutputStream o=c.getOutputStream()){o.write(bytes);}}
            int status=c.getResponseCode();InputStream stream=status>=400?c.getErrorStream():c.getInputStream();if(stream==null)throw new IOException("服务未返回数据（"+status+"）");
            byte[] data;try(InputStream in=stream;ByteArrayOutputStream out=new ByteArrayOutputStream()){byte[] b=new byte[8192];int n;while((n=in.read(b))!=-1){if(out.size()+n>20_000_000)throw new IOException("服务响应过大");out.write(b,0,n);}data=out.toByteArray();}
            JSONObject result=new JSONObject(new String(data,StandardCharsets.UTF_8));if(status<200||status>=300)throw new IOException(result.optString("error","通信失败（"+status+"）"));return result;
        } finally{c.disconnect();}
    }
}
