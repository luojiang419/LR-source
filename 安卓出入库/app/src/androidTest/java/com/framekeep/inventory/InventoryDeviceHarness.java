package com.framekeep.inventory;

import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.os.Bundle;
import android.app.Instrumentation;

import org.json.*;


import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.zip.*;


/** 相同签名的独立测试 APK：只在显式传入 action 时做账本备份与本轮测试行清理。 */
public final class InventoryDeviceHarness extends Instrumentation {
    private Bundle arguments;
    @Override public void onCreate(Bundle args){super.onCreate(args);arguments=args;start();}
    @Override public void onStart(){Bundle output=new Bundle();try{inventoryHarness();output.putString("result","passed");finish(0,output);}catch(Throwable failed){output.putString("error",failed.toString());finish(1,output);}}
    private void inventoryHarness() throws Exception {
        Bundle args=arguments;String action=args.getString("action");assertNotNull(action);
        Context context=getTargetContext();File database=context.getDatabasePath("inventory.sqlite3");assertTrue(database.isFile());
        File journal=new File(context.getFilesDir(),"codex-ocr-qa-journal.json"),external=context.getExternalFilesDir(null);assertNotNull(external);
        File resultFile=new File(external,"codex-ocr-qa-result.json");JSONObject result=new JSONObject().put("action",action).put("package",context.getPackageName());
        try(SQLiteDatabase db=SQLiteDatabase.openDatabase(database.toString(),null,SQLiteDatabase.OPEN_READWRITE)){
            if(action.equals("begin")){
                assertFalse("已有测试日志，需先检查上轮状态",journal.exists());JSONArray baseline=new JSONArray();try(Cursor c=db.rawQuery("SELECT id FROM records",null)){while(c.moveToNext())baseline.put(c.getString(0));}
                write(journal,new JSONObject().put("baseline",baseline).put("started",System.currentTimeMillis()).toString());
                try(Cursor checkpoint=db.rawQuery("PRAGMA wal_checkpoint(FULL)",null)){assertTrue(checkpoint.moveToFirst());}
                File backup=new File(external,"inventory-before-paddle.zip");
                try(ZipOutputStream zip=new ZipOutputStream(new FileOutputStream(backup))){add(zip,database,"inventory.sqlite3");for(String folder:new String[]{"images","draft"}){File dir=new File(context.getFilesDir(),folder);File[] files=dir.listFiles();if(files!=null)for(File file:files)if(file.isFile())add(zip,file,folder+"/"+file.getName());}}
                result.put("backup",backup.toString()).put("baselineCount",baseline.length());
            }else if(action.equals("offline")){
                assertTrue(journal.isFile());JSONObject session=new JSONObject(read(journal));android.content.SharedPreferences prefs=context.getSharedPreferences("inventory",0);
                if(!session.has("settings")){JSONObject saved=new JSONObject();for(String key:new String[]{"cloud","autoSync"})saved.put(key,new JSONObject().put("present",prefs.contains(key)).put("value",prefs.getBoolean(key,true)));session.put("settings",saved);write(journal,session.toString());}
                assertTrue(prefs.edit().putBoolean("cloud",false).putBoolean("autoSync",false).commit());
            }else if(action.equals("testDraft")){
                assertTrue(journal.isFile());JSONObject session=new JSONObject(read(journal));assertFalse(session.has("draftBefore"));
                session.put("draftBefore",context.getSharedPreferences("inventory",0).getString("draft","{}"));File saved=new File(context.getFilesDir(),"codex-ocr-qa-draft");saved.mkdirs();
                for(String kind:new String[]{"product","label"}){File original=new File(context.getFilesDir(),"draft/"+kind+".jpg");if(original.isFile())copy(original,new File(saved,kind+".jpg"));}
                write(journal,session.toString());assertTrue(context.getSharedPreferences("inventory",0).edit().putString("draft","{}").commit());result.put("draftProtected",true);
            }else if(action.equals("cleanup")){
                assertTrue("缺少本轮基线，拒绝清理",journal.isFile());JSONObject session=new JSONObject(read(journal));Set<String> baseline=new HashSet<>();JSONArray old=session.getJSONArray("baseline");for(int i=0;i<old.length();i++)baseline.add(old.getString(i));
                JSONArray ids=new JSONArray(args.getString("ids","[]"));assertTrue(ids.length()<=20);
                db.beginTransaction();try{
                    for(int i=0;i<ids.length();i++){String id=ids.getString(i);assertTrue(id.matches("[0-9a-fA-F-]{36}"));assertFalse("禁止删除测试前已有记录",baseline.contains(id));
                        try(Cursor c=db.rawQuery("SELECT at FROM records WHERE id=?",new String[]{id})){assertTrue(c.moveToFirst());assertTrue(java.time.OffsetDateTime.parse(c.getString(0)).toInstant().toEpochMilli()>=session.getLong("started")-5000);}
                        db.delete("records","id=?",new String[]{id});
                    }db.setTransactionSuccessful();
                }finally{db.endTransaction();}
                for(int i=0;i<ids.length();i++)for(String kind:new String[]{"product","label"}){File image=new File(context.getFilesDir(),"images/"+ids.getString(i)+"-"+kind+".jpg");if(image.exists())assertTrue(image.delete());}
                try(Cursor c=db.rawQuery("SELECT id FROM records",null)){Set<String> remaining=new HashSet<>();while(c.moveToNext())remaining.add(c.getString(0));assertTrue("原记录缺失",remaining.containsAll(baseline));}
                if(session.has("settings")){android.content.SharedPreferences.Editor edit=context.getSharedPreferences("inventory",0).edit();JSONObject saved=session.getJSONObject("settings");for(String key:new String[]{"cloud","autoSync"}){JSONObject value=saved.getJSONObject(key);if(value.getBoolean("present"))edit.putBoolean(key,value.getBoolean("value"));else edit.remove(key);}assertTrue(edit.commit());}
                if(session.has("draftBefore")){File saved=new File(context.getFilesDir(),"codex-ocr-qa-draft");for(String kind:new String[]{"product","label"}){File target=new File(context.getFilesDir(),"draft/"+kind+".jpg"),original=new File(saved,kind+".jpg");if(original.isFile()){target.getParentFile().mkdirs();copy(original,target);assertTrue(original.delete());}else if(target.exists())assertTrue(target.delete());}if(saved.exists())assertTrue(saved.delete());assertTrue(context.getSharedPreferences("inventory",0).edit().putString("draft",session.getString("draftBefore")).commit());result.put("draftRestored",true);}
                assertTrue(journal.delete());result.put("removed",ids.length());
            }else assertEquals("inspect",action);
            JSONArray rows=new JSONArray();
            try(Cursor c=db.rawQuery("SELECT id,mode,sku,at,source,ocr_status,ocr_revision,synced,deleted_at,product_sha,label_sha,ocr_result FROM records ORDER BY at,id",null)){
                while(c.moveToNext()){JSONObject row=new JSONObject();for(String key:c.getColumnNames()){int index=c.getColumnIndexOrThrow(key);if(key.equals("ocr_result")){row.put("requestedModel",new JSONObject(c.getString(index)).optString("requestedModel"));continue;}row.put(key,c.isNull(index)?JSONObject.NULL:c.getString(index));}rows.put(row);}
            }
            result.put("records",rows);
            android.content.SharedPreferences prefs=context.getSharedPreferences("inventory",0);result.put("ocrModel",prefs.getString("ocrModel","paddle-small")).put("cloud",prefs.getBoolean("cloud",true)).put("autoSync",prefs.getBoolean("autoSync",true));
        }
        write(resultFile,result.toString(2));Bundle status=new Bundle();status.putString("qa_result",resultFile.toString());sendStatus(0,status);
    }
    static String read(File file) throws IOException {StringBuilder text=new StringBuilder();try(BufferedReader reader=new BufferedReader(new InputStreamReader(new FileInputStream(file),StandardCharsets.UTF_8))){String line;while((line=reader.readLine())!=null)text.append(line);}return text.toString();}
    static void write(File file,String value) throws IOException {try(OutputStream out=new FileOutputStream(file)){out.write(value.getBytes(StandardCharsets.UTF_8));}}
    static void add(ZipOutputStream zip,File file,String name) throws IOException {zip.putNextEntry(new ZipEntry(name));try(InputStream in=new FileInputStream(file)){byte[] bytes=new byte[65536];int size;while((size=in.read(bytes))!=-1)zip.write(bytes,0,size);}zip.closeEntry();}
    static void copy(File from,File to) throws IOException {try(InputStream input=new FileInputStream(from);OutputStream output=new FileOutputStream(to)){byte[] bytes=new byte[65536];int size;while((size=input.read(bytes))!=-1)output.write(bytes,0,size);}}
    static void assertTrue(boolean value){if(!value)throw new IllegalStateException("校验失败");}
    static void assertTrue(String message,boolean value){if(!value)throw new IllegalStateException(message);}
    static void assertFalse(boolean value){assertTrue(!value);}
    static void assertFalse(String message,boolean value){assertTrue(message,!value);}
    static void assertNotNull(Object value){assertTrue(value!=null);}
    static void assertEquals(Object expected,Object actual){assertTrue(java.util.Objects.equals(expected,actual));}

}
