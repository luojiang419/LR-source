package com.framekeep.inventory;

import android.content.Context;
import android.os.Bundle;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.*;
import org.junit.*;
import org.junit.runner.RunWith;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import static org.junit.Assert.*;

/** 只运行在明确指定的独立验证包；模型与样本放在该包私有目录。 */
@RunWith(AndroidJUnit4.class)
public class PaddlePhoneBenchmarkTest {
    static String read(File file) throws IOException {StringBuilder text=new StringBuilder();try(BufferedReader reader=new BufferedReader(new InputStreamReader(new FileInputStream(file),StandardCharsets.UTF_8))){String line;while((line=reader.readLine())!=null)text.append(line).append('\n');}return text.toString();}
    @Test public void compareModelsOnDevice() throws Exception {
        Context context=InstrumentationRegistry.getInstrumentation().getTargetContext();
        Assume.assumeTrue(context.getPackageName().endsWith(".verification"));
        Bundle args=InstrumentationRegistry.getArguments();String requested=args.getString("models");Assume.assumeNotNull(requested);
        File root=new File(context.getFilesDir(),"paddle-benchmark"),fixtures=new File(root,"fixtures");
        JSONArray samples=new JSONArray(read(new File(fixtures,"fixtures.json")));assertTrue(samples.length()>0);
        for(String model:requested.split(",")){
            JSONArray results=new JSONArray();JSONObject report=new JSONObject().put("model",model).put("device",android.os.Build.MODEL).put("sdk",android.os.Build.VERSION.SDK_INT).put("results",results);
            File destination=new File(root,"result-"+model+".json");
            for(int i=0;i<samples.length();i++){
                JSONObject sample=samples.getJSONObject(i),result=new JSONObject();File image=new File(fixtures,sample.getString("file"));long start=System.nanoTime();
                try{
                    if(model.startsWith("mlkit"))result=new SkuRecognizer(context).offline(image,"LC,AB",model.equals("mlkit-chinese"));
                    else {String family=model.equals("v5")?"PP-OCRv5_mobile":"PP-OCRv6_"+model;result=PaddleSkuRecognizer.recognize(context,image,"LC,AB",new File(root,"models").getAbsolutePath(),family+"_det",family+"_rec");}
                }catch(Throwable failed){result.put("error",failed.getClass().getName()+": "+failed.getMessage());}
                result.put("sample",sample).put("elapsedMs",(System.nanoTime()-start)/1_000_000);results.put(result);
                try(OutputStream out=new FileOutputStream(destination)){out.write(report.toString(2).getBytes(StandardCharsets.UTF_8));}
                Bundle status=new Bundle();status.putString("case",model+" / "+sample.getString("file"));status.putString("sku",result.optString("sku"));status.putString("preferred",result.optJSONArray("preferred")==null?"":result.getJSONArray("preferred").toString());status.putLong("elapsedMs",result.getLong("elapsedMs"));if(result.has("error"))status.putString("error",result.getString("error"));InstrumentationRegistry.getInstrumentation().sendStatus(0,status);
            }
            assertEquals(samples.length(),results.length());
        }
    }
}
