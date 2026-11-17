package com.framekeep.inventory;

import android.app.Instrumentation;
import android.content.*;
import android.content.pm.PackageInfo;
import android.database.Cursor;
import android.graphics.Bitmap;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.*;
import org.junit.Assume;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.*;
import static org.junit.Assert.*;

/** 分 prepare/verify/cleanup 三阶段运行，阶段之间用 install -r 覆盖升级，禁止卸载/清数据。 */
@RunWith(AndroidJUnit4.class)
public class InventoryUpgradeTest {
    final Instrumentation instrumentation=InstrumentationRegistry.getInstrumentation();
    final Context context=instrumentation.getTargetContext();
    final File journal=new File(context.getFilesDir(),"record-upgrade-test.json");
    final SharedPreferences prefs=context.getSharedPreferences("inventory",0);
    void write(File path,JSONObject value) throws Exception {Files.write(path.toPath(),value.toString().getBytes(StandardCharsets.UTF_8));}
    JSONObject read() throws Exception {return new JSONObject(new String(Files.readAllBytes(journal.toPath()),StandardCharsets.UTF_8));}
    String digest(String text) throws Exception {java.security.MessageDigest hash=java.security.MessageDigest.getInstance("SHA-256");StringBuilder result=new StringBuilder();for(byte b:hash.digest(text.getBytes(StandardCharsets.UTF_8)))result.append(String.format(Locale.ROOT,"%02x",b&255));return result.toString();}
    void imageHashes(File directory,String prefix,TreeMap<String,String> hashes) throws Exception {File[] files=directory.listFiles();if(files!=null)for(File f:files){if(f.isDirectory())imageHashes(f,prefix+f.getName()+"/",hashes);else hashes.put(prefix+f.getName(),InventoryStore.sha(f));}}
    JSONObject snapshot(InventoryStore store) throws Exception {
        JSONArray rows=new JSONArray();try(Cursor cursor=store.getReadableDatabase().rawQuery("SELECT * FROM records ORDER BY id",null)){while(cursor.moveToNext())rows.put(store.row(cursor));}
        TreeMap<String,String> images=new TreeMap<>();imageHashes(store.images,"images/",images);imageHashes(new File(context.getFilesDir(),"draft"),"draft/",images);
        return new JSONObject().put("rows",rows).put("images",new JSONObject(images)).put("settings",new JSONObject(new TreeMap<>(prefs.getAll())));
    }
    void equalSnapshot(JSONObject expected,JSONObject actual) throws Exception {
        assertEquals("记录字段必须完整保留",expected.getJSONArray("rows").toString(),actual.getJSONArray("rows").toString());
        for(String section:new String[]{"images","settings"}){JSONObject a=expected.getJSONObject(section),b=actual.getJSONObject(section);assertEquals(section+" 数量变化",a.length(),b.length());Iterator<String> keys=a.keys();while(keys.hasNext()){String key=keys.next();assertEquals(section+" 内容变化: "+key,String.valueOf(a.get(key)),String.valueOf(b.get(key)));}}
    }
    File photo(String id,String kind) throws Exception {File file=new File(context.getCacheDir(),id+"-"+kind+".jpg");Bitmap bitmap=Bitmap.createBitmap(24,32,Bitmap.Config.ARGB_8888);bitmap.eraseColor(kind.equals("product")?0xff557788:0xffaaaacc);try(OutputStream out=new FileOutputStream(file)){assertTrue(bitmap.compress(Bitmap.CompressFormat.JPEG,90,out));}finally{bitmap.recycle();}return file;}
    @Test public void retainAllDataAcrossPackageUpgrade() throws Exception {
        String phase=InstrumentationRegistry.getArguments().getString("upgradePhase");Assume.assumeNotNull(phase);
        try(InventoryStore store=new InventoryStore(context)){
            if(phase.equals("prepare")){
                assertFalse("已有升级测试快照，需先完成或清理",journal.exists());PackageInfo installed=context.getPackageManager().getPackageInfo(context.getPackageName(),0);assertEquals(1,installed.getLongVersionCode());
                JSONObject session=new JSONObject().put("autoSyncPresent",prefs.contains("autoSync")).put("autoSync",prefs.getBoolean("autoSync",true)).put("draftPresent",prefs.contains("draft")).put("draft",prefs.getString("draft","{}")).put("firstInstallTime",installed.firstInstallTime).put("uid",context.getApplicationInfo().uid).put("ids",new JSONArray());write(journal,session);
                prefs.edit().putBoolean("autoSync",false).commit();
                for(int index=0;index<4;index++){
                    String id=UUID.randomUUID().toString();session.getJSONArray("ids").put(id);write(journal,session);
                    store.save(id,index==1?"out":"in","LC-UPGRADE-"+index,photo(id,"product"),photo(id,"label"),"升级保留测试","manual","2026-11-09T10:00:00+08:00");
                    if(index==0)store.acknowledge(id);if(index==2)store.trash(id);
                    if(index==3)store.getWritableDatabase().execSQL("UPDATE records SET sku='',ocr_status='review',ocr_result=? WHERE id=?",new Object[]{new JSONObject().put("text","LC-A LC-B").put("candidates",new JSONArray().put("LC-A").put("LC-B")).toString(),id});
                }
                File draft=new File(context.getFilesDir(),"draft/product.jpg");
                if(!draft.exists()&&!new JSONObject(prefs.getString("draft","{}")).has("id")){
                    session.put("fixtureDraft",true);write(journal,session);draft.getParentFile().mkdirs();Files.copy(photo(UUID.randomUUID().toString(),"product").toPath(),draft.toPath());
                    prefs.edit().putString("draft",new JSONObject().put("id",UUID.randomUUID().toString()).put("mode","in").put("stage","label").put("at","2026-11-09T10:00:00+08:00").toString()).commit();
                }
                session.put("snapshot",snapshot(store));write(journal,session);
            }else if(phase.equals("verify")){
                JSONObject session=read();PackageInfo installed=context.getPackageManager().getPackageInfo(context.getPackageName(),0);assertEquals(2,installed.getLongVersionCode());assertEquals("1.0.1",installed.versionName);assertEquals("升级不得重新安装",session.getLong("firstInstallTime"),installed.firstInstallTime);assertEquals(session.getInt("uid"),context.getApplicationInfo().uid);
                equalSnapshot(session.getJSONObject("snapshot"),snapshot(store));
                MainActivity activity=(MainActivity)instrumentation.startActivitySync(new Intent(context,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));instrumentation.waitForIdleSync();Thread.sleep(700);
                equalSnapshot(session.getJSONObject("snapshot"),snapshot(store));instrumentation.runOnMainSync(activity::finish);instrumentation.waitForIdleSync();
                JSONObject result=new JSONObject().put("passed",true).put("from","1.0.0 (1)").put("to","1.0.1 (2)").put("records",session.getJSONObject("snapshot").getJSONArray("rows").length()).put("imageAndDraftFiles",session.getJSONObject("snapshot").getJSONObject("images").length()).put("firstInstallTimeUnchanged",true).put("uidUnchanged",true).put("recordsSha256",digest(snapshot(store).getJSONArray("rows").toString())).put("allFieldsImagesDraftSettingsUnchanged",true);
                write(new File(context.getExternalFilesDir(null),"record-upgrade-result.json"),result);
            }else if(phase.equals("cleanup")){
                JSONObject session=read();JSONArray ids=session.getJSONArray("ids");for(int i=0;i<ids.length();i++){String id=ids.getString(i);store.getWritableDatabase().delete("records","id=?",new String[]{id});store.getWritableDatabase().delete("sync_changes","record_id=?",new String[]{id});for(String kind:new String[]{"product","label"}){File f=new File(store.images,id+"-"+kind+".jpg");if(f.exists())assertTrue(f.delete());}}
                SharedPreferences.Editor restore=prefs.edit();if(session.getBoolean("autoSyncPresent"))restore.putBoolean("autoSync",session.getBoolean("autoSync"));else restore.remove("autoSync");
                if(session.optBoolean("fixtureDraft")){assertTrue(new File(context.getFilesDir(),"draft/product.jpg").delete());if(session.getBoolean("draftPresent"))restore.putString("draft",session.getString("draft"));else restore.remove("draft");}assertTrue(restore.commit());assertTrue(journal.delete());
            }else fail("未知阶段");
        }
    }
}
