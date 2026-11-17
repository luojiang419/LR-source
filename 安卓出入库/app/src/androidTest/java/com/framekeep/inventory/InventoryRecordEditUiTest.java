package com.framekeep.inventory;

import android.app.Instrumentation;
import android.content.*;
import android.graphics.Bitmap;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.*;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.io.*;
import java.util.UUID;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicReference;
import static org.junit.Assert.*;

/** 真实 WebView -> 原生桥 -> SQLite 的编辑保存和重开验证。 */
@RunWith(AndroidJUnit4.class)
public class InventoryRecordEditUiTest {
    final Instrumentation instrumentation=InstrumentationRegistry.getInstrumentation();
    MainActivity activity;
    Object js(String expression) throws Exception {
        CountDownLatch ready=new CountDownLatch(1);AtomicReference<String> result=new AtomicReference<>();
        instrumentation.runOnMainSync(()->activity.web.evaluateJavascript(expression,value->{result.set(value);ready.countDown();}));
        assertTrue("WebView 回调超时",ready.await(8,TimeUnit.SECONDS));return new JSONTokener(result.get()).nextValue();
    }
    void until(String expression) throws Exception {
        long end=System.currentTimeMillis()+10000;
        do{if(Boolean.TRUE.equals(js(expression)))return;Thread.sleep(100);}while(System.currentTimeMillis()<end);
        fail("页面等待失败: "+expression);
    }
    void launch(Context context) throws Exception {
        Intent intent=new Intent(context,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        activity=(MainActivity)instrumentation.startActivitySync(intent);
        until("document.readyState==='complete' && !!document.querySelector('.bottom-nav')");
    }
    void screenshot(Context context,String name) throws Exception {
        Bitmap image=instrumentation.getUiAutomation().takeScreenshot();assertNotNull(image);
        try(OutputStream out=new FileOutputStream(new File(context.getExternalFilesDir(null),name))){assertTrue(image.compress(Bitmap.CompressFormat.PNG,100,out));}finally{image.recycle();}
    }
    @Test public void editSkuThroughActualDialogThenReopenApp() throws Exception {
        Context context=instrumentation.getTargetContext();SharedPreferences prefs=context.getSharedPreferences("inventory",0);
        boolean hadAutoSync=prefs.contains("autoSync"),autoSync=prefs.getBoolean("autoSync",true);prefs.edit().putBoolean("autoSync",false).commit();
        String id=UUID.randomUUID().toString();InventoryStore ledger=null;
        try {
            launch(context);ledger=new InventoryStore(context);File product=new File(context.getCacheDir(),id+"-p.jpg"),label=new File(context.getCacheDir(),id+"-l.jpg");
            Bitmap bitmap=Bitmap.createBitmap(160,200,Bitmap.Config.ARGB_8888);bitmap.eraseColor(0xff66829a);
            try{for(File file:new File[]{product,label})try(OutputStream out=new FileOutputStream(file)){bitmap.compress(Bitmap.CompressFormat.JPEG,90,out);}}finally{bitmap.recycle();}
            JSONObject before=ledger.save(id,"out","LC-UI-BEFORE",product,label,"LC-UI-BEFORE","test","2026-11-09T10:00:00+08:00");
            js("document.querySelector('.bottom-nav [data-nav=records]').click()");String row="document.querySelector('.record-swipe[data-id=\""+id+"\"]')";
            until("!!"+row);js(row+".querySelector('[data-record-edit]').click()");until("document.querySelector('#record-review-dialog').open");
            assertEquals("LC-UI-BEFORE",js("document.querySelector('#record-sku').value"));assertEquals("编辑款号",js("document.querySelector('#record-review-title').textContent"));
            screenshot(context,"record-edit-popup.png");
            js("document.querySelector('#record-sku').value=' lc-ui-after ';document.querySelector('#record-confirm').click()");
            until("!document.querySelector('#record-review-dialog').open && "+row+"?.querySelector('strong').textContent==='LC-UI-AFTER'");
            JSONObject after=ledger.record(id);assertEquals("LC-UI-AFTER",after.getString("sku"));for(String key:new String[]{"id","at","mode","product_sha","label_sha"})assertEquals(before.getString(key),after.getString(key));
            screenshot(context,"record-edit-saved.png");
            instrumentation.runOnMainSync(()->activity.finish());instrumentation.waitForIdleSync();launch(context);
            js("document.querySelector('.bottom-nav [data-nav=records]').click()");until(""+row+"?.querySelector('strong').textContent==='LC-UI-AFTER'");
            js(row+".querySelector('[data-record-edit]').click()");until("document.querySelector('#record-review-dialog').open");assertEquals("LC-UI-AFTER",js("document.querySelector('#record-sku').value"));
        } finally {
            if(activity!=null)instrumentation.runOnMainSync(()->activity.finish());instrumentation.waitForIdleSync();
            if(ledger!=null){ledger.getWritableDatabase().delete("records","id=?",new String[]{id});for(String kind:new String[]{"product","label"}){File image=new File(ledger.images,id+"-"+kind+".jpg");if(image.exists())assertTrue(image.delete());}ledger.close();}
            SharedPreferences.Editor restore=prefs.edit();if(hadAutoSync)restore.putBoolean("autoSync",autoSync);else restore.remove("autoSync");restore.commit();
        }
    }
}
