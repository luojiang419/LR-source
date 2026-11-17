package com.framekeep.inventory;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import androidx.exifinterface.media.ExifInterface;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.*;
import org.junit.runner.RunWith;
import java.io.*;
import java.nio.file.Files;
import java.time.ZoneId;
import java.util.UUID;
import java.util.zip.ZipFile;
import static org.junit.Assert.*;

/** 独立测试账本，验证 XLSX 内嵌图片与保存的原图逐字节一致。 */
@RunWith(AndroidJUnit4.class)
public class WorkbookTest {
    Context context;InventoryStore store;String database;
    @Before public void setup(){
        context=InstrumentationRegistry.getInstrumentation().getTargetContext();
        if(!context.getSharedPreferences("inventory",0).contains("device"))context.getSharedPreferences("inventory",0).edit().putString("device",UUID.randomUUID().toString()).commit();
        String key="workbook-test-"+UUID.randomUUID();database=key+".db";
        store=new InventoryStore(context,database,key);
    }
    @After public void cleanup() throws Exception {
        store.close();context.deleteDatabase(database);
        File[] files=store.images.listFiles();if(files!=null)for(File file:files)assertTrue(file.delete());
        assertTrue(store.images.delete());
    }
    File photo(String name,int width,int height) throws Exception {
        File file=new File(store.images,name);Bitmap bitmap=Bitmap.createBitmap(width,height,Bitmap.Config.ARGB_8888);
        bitmap.eraseColor(0xff557799);
        try(OutputStream out=new FileOutputStream(file)){assertTrue(bitmap.compress(Bitmap.CompressFormat.JPEG,97,out));}finally{bitmap.recycle();}
        ExifInterface exif=new ExifInterface(file);exif.setAttribute(ExifInterface.TAG_USER_COMMENT,"original-export-test");exif.saveAttributes();
        return file;
    }
    @Test public void allAndDateExportsEmbedOriginalPhotosWithoutResizingOrReencoding() throws Exception {
        String id=UUID.randomUUID().toString();
        store.save(id,"in","LC-ORIGINAL",photo("product.jpg",3072,128),photo("label.jpg",128,2304),"","offline","2026-11-16T10:00:00+08:00");
        byte[][] originals={Files.readAllBytes(store.image(id,"product").toPath()),Files.readAllBytes(store.image(id,"label").toPath())};
        ExportRange[] ranges={ExportRange.all(),ExportRange.dates("2026-11-16","2026-11-16",ZoneId.of("Asia/Shanghai"))};
        for(int n=0;n<ranges.length;n++){
            File target=new File(store.images,"export-"+n+".xlsx");Workbook.write(target,store,"","",ranges[n]);
            try(ZipFile zip=new ZipFile(target)){
                for(int i=0;i<2;i++)try(InputStream input=zip.getInputStream(zip.getEntry("xl/media/image"+(i+1)+".jpg"))){
                    ByteArrayOutputStream bytes=new ByteArrayOutputStream();byte[] buffer=new byte[8192];int count;
                    while((count=input.read(buffer))!=-1)bytes.write(buffer,0,count);
                    byte[] embedded=bytes.toByteArray();assertArrayEquals(originals[i],embedded);
                    BitmapFactory.Options bounds=new BitmapFactory.Options();bounds.inJustDecodeBounds=true;BitmapFactory.decodeByteArray(embedded,0,embedded.length,bounds);
                    assertEquals(i==0?3072:128,bounds.outWidth);assertEquals(i==0?128:2304,bounds.outHeight);
                }
                assertNull(zip.getEntry("xl/media/image3.jpg"));
            }
        }
        assertArrayEquals(originals[0],Files.readAllBytes(store.image(id,"product").toPath()));
        assertArrayEquals(originals[1],Files.readAllBytes(store.image(id,"label").toPath()));
    }
}
