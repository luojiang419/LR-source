package com.framekeep.inventory;

import android.Manifest;
import android.content.*;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;

/** 不导出记录；只有持有系统 DUMP 权限的已授权 ADB 调用可获取临时 USB 会话。 */
public final class UsbBridgeProvider extends ContentProvider {
    @Override public boolean onCreate(){return true;}
    @Override public Bundle call(String method,String arg,Bundle extras){
        Context context=getContext();if(context==null||context.checkCallingPermission(Manifest.permission.DUMP)!=PackageManager.PERMISSION_GRANTED)throw new SecurityException("仅允许已授权 USB 调用");
        if(method.equals("start"))return UsbTransferService.connect(context);
        if(method.equals("stop")){UsbTransferService.disconnect(context);return Bundle.EMPTY;}
        throw new IllegalArgumentException("未知 USB 操作");
    }
    @Override public Cursor query(Uri uri,String[] p,String s,String[] a,String o){throw new UnsupportedOperationException();}
    @Override public String getType(Uri uri){return null;}
    @Override public Uri insert(Uri uri,ContentValues values){throw new UnsupportedOperationException();}
    @Override public int delete(Uri uri,String s,String[] a){throw new UnsupportedOperationException();}
    @Override public int update(Uri uri,ContentValues v,String s,String[] a){throw new UnsupportedOperationException();}
}
