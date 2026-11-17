package com.framekeep.inventory;

import android.content.*;
import android.database.Cursor;
import android.database.sqlite.*;
import org.json.*;
import java.io.*;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.time.OffsetDateTime;
import java.util.*;

final class InventoryStore extends SQLiteOpenHelper {
    static final long TRASH_RETENTION_MS=30L*24*60*60*1000;
    final File images;
    final String device;
    InventoryStore(Context context) {
        this(context,"inventory.sqlite3","images");
    }
    InventoryStore(Context context,String database,String imageFolder) {
        super(context,database,null,4);
        images=new File(context.getFilesDir(),imageFolder);images.mkdirs();
        SharedPreferences p=context.getSharedPreferences("inventory",0);
        device=p.getString("device","");
        if(device.isEmpty())throw new IllegalStateException("设备未初始化");
        setWriteAheadLoggingEnabled(true);
        getWritableDatabase().execSQL("CREATE TABLE IF NOT EXISTS maintenance(key TEXT PRIMARY KEY,value INTEGER NOT NULL)");
    }
    @Override public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE records(id TEXT PRIMARY KEY, device TEXT NOT NULL, mode TEXT NOT NULL, sku TEXT NOT NULL, at TEXT NOT NULL, raw TEXT NOT NULL, source TEXT NOT NULL, product_sha TEXT NOT NULL, label_sha TEXT NOT NULL, synced INTEGER NOT NULL DEFAULT 0, deleted_at INTEGER)");
        db.execSQL("CREATE INDEX records_time ON records(at DESC,id)");
        db.execSQL("CREATE INDEX records_deleted ON records(deleted_at)");
        addOcrColumns(db);
        addSyncChanges(db);
    }
    @Override public void onUpgrade(SQLiteDatabase db,int oldVersion,int newVersion) {
        if(oldVersion<2){db.execSQL("ALTER TABLE records ADD COLUMN deleted_at INTEGER");db.execSQL("CREATE INDEX records_deleted ON records(deleted_at)");}
        if(oldVersion<3)addOcrColumns(db);
        if(oldVersion<4){addSyncChanges(db);db.execSQL("INSERT INTO sync_changes(record_id) SELECT id FROM records WHERE deleted_at IS NULL AND ocr_status='ready' ORDER BY at,id");}
    }
    private void addSyncChanges(SQLiteDatabase db){
        db.execSQL("CREATE TABLE sync_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,record_id TEXT NOT NULL)");
        db.execSQL("CREATE TRIGGER sync_ready_insert AFTER INSERT ON records WHEN NEW.deleted_at IS NULL AND NEW.ocr_status='ready' BEGIN INSERT INTO sync_changes(record_id) VALUES(NEW.id); END");
        db.execSQL("CREATE TRIGGER sync_ready_update AFTER UPDATE OF ocr_status,deleted_at ON records WHEN NEW.deleted_at IS NULL AND NEW.ocr_status='ready' AND (OLD.ocr_status!='ready' OR OLD.deleted_at IS NOT NULL) BEGIN INSERT INTO sync_changes(record_id) VALUES(NEW.id); END");
    }
    synchronized JSONObject changes(long after,int limit) throws Exception {
        JSONArray rows=new JSONArray();long next=Math.max(0,after),latest=0;
        try(Cursor c=getReadableDatabase().rawQuery("SELECT e.seq,r.* FROM sync_changes e LEFT JOIN records r ON r.id=e.record_id WHERE e.seq>? ORDER BY e.seq LIMIT ?",new String[]{String.valueOf(next),String.valueOf(Math.max(1,Math.min(limit,100)))})){
            while(c.moveToNext()){next=c.getLong(0);if(c.isNull(c.getColumnIndexOrThrow("id"))||!c.isNull(c.getColumnIndexOrThrow("deleted_at"))||!c.getString(c.getColumnIndexOrThrow("ocr_status")).equals("ready"))continue;JSONObject row=row(c);row.put("revision",row.optInt("ocr_revision"));for(String key:new String[]{"ocr_status","ocr_result","ocr_revision","deleted_at"})row.remove(key);rows.put(row);}
        }
        try(Cursor c=getReadableDatabase().rawQuery("SELECT coalesce(max(seq),0) FROM sync_changes",null)){c.moveToFirst();latest=c.getLong(0);}
        return new JSONObject().put("records",rows).put("next",next).put("done",next>=latest);
    }
    private void addOcrColumns(SQLiteDatabase db){
        db.execSQL("ALTER TABLE records ADD COLUMN ocr_status TEXT NOT NULL DEFAULT 'ready'");
        db.execSQL("ALTER TABLE records ADD COLUMN ocr_result TEXT NOT NULL DEFAULT '{}'");
        db.execSQL("ALTER TABLE records ADD COLUMN ocr_revision INTEGER NOT NULL DEFAULT 0");
        db.execSQL("CREATE INDEX records_ocr ON records(ocr_status,deleted_at)");
    }
    static String sha(File file) throws Exception {
        MessageDigest d=MessageDigest.getInstance("SHA-256");
        try(InputStream in=new FileInputStream(file)){byte[] b=new byte[65536];int n;while((n=in.read(b))!=-1)d.update(b,0,n);}
        StringBuilder s=new StringBuilder();for(byte b:d.digest())s.append(String.format(Locale.ROOT,"%02x",b&255));return s.toString();
    }
    synchronized JSONObject save(String id,String mode,String sku,File product,File label,String raw,String source,String at) throws Exception {
        return saveInternal(id,mode,validSku(sku),product,label,raw,source,at,"ready","");
    }
    synchronized JSONObject saveCaptured(String id,String mode,File product,File label,String at) throws Exception {
        return saveCaptured(id,mode,product,label,at,OcrModels.DEFAULT);
    }
    synchronized JSONObject saveCaptured(String id,String mode,File product,File label,String at,String model) throws Exception {
        return saveInternal(id,mode,"",product,label,"","pending",at,"pending",OcrModels.require(model));
    }
    static String validSku(String sku){
        sku=sku.trim().toUpperCase(Locale.ROOT);
        if(!sku.matches("[A-Z0-9][A-Z0-9._/\\-]{0,79}"))throw new IllegalArgumentException("款号需为 1–80 位字母、数字或 . _ / -");return sku;
    }
    private JSONObject saveInternal(String id,String mode,String sku,File product,File label,String raw,String source,String at,String status,String model) throws Exception {
        if(cleanupPending())throw new IOException("请先完成记录清理，再开始新的扫描");
        if(!mode.equals("in")&&!mode.equals("out"))throw new IllegalArgumentException("出入库模式无效");
        try(Cursor c=getReadableDatabase().rawQuery("SELECT * FROM records WHERE id=?",new String[]{id})){if(c.moveToFirst())return row(c);}
        if(!product.isFile()||!label.isFile())throw new IOException("请先拍摄产品与标签两张图片");
        String ps=sha(product),ls=sha(label);File dest1=new File(images,id+"-product.jpg"),dest2=new File(images,id+"-label.jpg");
        try {
            Files.copy(product.toPath(),dest1.toPath(),java.nio.file.StandardCopyOption.REPLACE_EXISTING);Files.copy(label.toPath(),dest2.toPath(),java.nio.file.StandardCopyOption.REPLACE_EXISTING);
            ContentValues v=new ContentValues();v.put("id",id);v.put("device",device);v.put("mode",mode);v.put("sku",sku);v.put("at",at);v.put("raw",raw);v.put("source",source);v.put("product_sha",ps);v.put("label_sha",ls);
            v.put("ocr_status",status);
            if(status.equals("pending"))v.put("ocr_result",new JSONObject().put("requestedModel",model).toString());
            getWritableDatabase().insertOrThrow("records",null,v);
        } catch(Exception e){dest1.delete();dest2.delete();throw e;}
        product.delete();label.delete();
        try(Cursor c=getReadableDatabase().rawQuery("SELECT * FROM records WHERE id=?",new String[]{id})){c.moveToFirst();return row(c);}
    }
    JSONObject row(Cursor c) throws JSONException {
        JSONObject o=new JSONObject();for(String k:c.getColumnNames())o.put(k,k.equals("synced")?c.getInt(c.getColumnIndexOrThrow(k)):c.getString(c.getColumnIndexOrThrow(k)));return o;
    }
    synchronized JSONObject record(String id) throws Exception {
        try(Cursor c=getReadableDatabase().rawQuery("SELECT * FROM records WHERE id=? AND deleted_at IS NULL",new String[]{id})){
            if(!c.moveToFirst())throw new IOException("记录已删除或不存在");JSONObject result=row(c);result.put("recognition",new JSONObject(result.getString("ocr_result")));return result;
        }
    }
    synchronized JSONArray queued() throws JSONException {
        JSONArray rows=new JSONArray();try(Cursor c=getReadableDatabase().rawQuery("SELECT * FROM records WHERE ocr_status='pending' AND deleted_at IS NULL ORDER BY at,id",null)){while(c.moveToNext())rows.put(row(c));}return rows;
    }
    synchronized boolean finishRecognition(String id,int revision,JSONObject result) throws Exception {
        boolean automatic=result.optBoolean("automatic");String sku=automatic?validSku(result.optString("sku")):"";
        ContentValues v=new ContentValues();v.put("sku",sku);v.put("ocr_status",automatic?"ready":"review");v.put("ocr_result",result.toString());v.put("raw",result.optString("text"));v.put("source",result.optString("source","offline"));v.put("synced",0);
        return getWritableDatabase().update("records",v,"id=? AND ocr_revision=? AND ocr_status='pending' AND deleted_at IS NULL",new String[]{id,String.valueOf(revision)})==1;
    }
    synchronized JSONObject confirmRecord(String id,int revision,String sku) throws Exception {
        return setRecordSku(id,revision,sku,true);
    }
    synchronized JSONObject updateRecordSku(String id,int revision,String sku) throws Exception {
        return setRecordSku(id,revision,sku,false);
    }
    private JSONObject setRecordSku(String id,int revision,String sku,boolean reviewOnly) throws Exception {
        if(cleanupPending())throw new IOException("请先完成记录清理");
        sku=validSku(sku);SQLiteDatabase db=getWritableDatabase();db.beginTransaction();
        try {
            JSONObject before=record(id);String status=before.getString("ocr_status");
            if(before.getInt("ocr_revision")!=revision||(reviewOnly&&!status.equals("review")))throw new IOException("记录已变化，请重新打开核对");
            if(status.equals("ready")&&before.getString("sku").equals(sku)){db.setTransactionSuccessful();return before;}
            ContentValues v=new ContentValues();v.put("sku",sku);v.put("ocr_status","ready");v.put("source","manual");v.put("synced",0);v.put("ocr_revision",revision+1);
            if(db.update("records",v,"id=? AND ocr_revision=? AND deleted_at IS NULL",new String[]{id,String.valueOf(revision)})!=1)throw new IOException("记录已变化，请重新打开核对");
            // pending/review -> ready 已有触发器；已确认款号修改需另记增量。
            if(status.equals("ready"))db.execSQL("INSERT INTO sync_changes(record_id) VALUES(?)",new Object[]{id});
            JSONObject result=record(id);db.setTransactionSuccessful();return result;
        } finally {db.endTransaction();}
    }
    synchronized JSONObject retryRecord(String id,int revision) throws Exception {
        return retryRecord(id,revision,OcrModels.DEFAULT);
    }
    synchronized JSONObject retryRecord(String id,int revision,String model) throws Exception {
        if(cleanupPending())throw new IOException("请先完成记录清理");
        ContentValues v=new ContentValues();v.put("ocr_status","pending");v.put("ocr_revision",revision+1);
        v.put("ocr_result",new JSONObject().put("requestedModel",OcrModels.require(model)).toString());
        if(getWritableDatabase().update("records",v,"id=? AND ocr_revision=? AND ocr_status='review' AND deleted_at IS NULL",new String[]{id,String.valueOf(revision)})!=1)throw new IOException("记录已变化，请重新打开核对");return record(id);
    }
    synchronized JSONArray list(String mode,String query,int offset,int limit,boolean pending) throws JSONException {
        return list(mode,query,offset,limit,pending,ExportRange.all());
    }
    synchronized JSONArray list(String mode,String query,int offset,int limit,boolean pending,ExportRange range) throws JSONException {
        return list(mode,query,offset,limit,pending,range,false);
    }
    synchronized JSONArray syncList(int offset,int limit,boolean pending) throws JSONException {return list("","",offset,limit,pending,ExportRange.all(),true);}
    private JSONArray list(String mode,String query,int offset,int limit,boolean pending,ExportRange range,boolean readyOnly) throws JSONException {
        ArrayList<String> args=new ArrayList<>();String where="deleted_at IS NULL";
        if(readyOnly)where+=" AND ocr_status='ready'";
        if(mode.equals("in")||mode.equals("out")){where+=" AND mode=?";args.add(mode);}
        if(!query.isEmpty()){where+=" AND instr(sku,?)>0";args.add(query.toUpperCase(Locale.ROOT));}
        if(pending)where+=" AND synced=0";
        if(!range.from.isEmpty()){
            // SQLite 时间计算只有毫秒精度；先粗筛，再按原始偏移时间精确判断，避免午夜附近纳秒被舍入到次日。
            where+=" AND julianday(at)>=julianday(?)-0.00002 AND julianday(at)<julianday(?)+0.00002";args.add(range.from);args.add(range.until);
            JSONArray selected=new JSONArray();int skipped=0;
            try(Cursor c=getReadableDatabase().rawQuery("SELECT * FROM records WHERE "+where+" ORDER BY at DESC,id",args.toArray(new String[0]))){
                while(c.moveToNext()){if(!range.contains(c.getString(c.getColumnIndexOrThrow("at"))))continue;if(skipped++<Math.max(0,offset))continue;selected.put(row(c));if(selected.length()>=Math.min(limit,500))break;}
            }
            return selected;
        }
        JSONArray a=new JSONArray();
        try(Cursor c=getReadableDatabase().rawQuery("SELECT * FROM records WHERE "+where+" ORDER BY at DESC,id LIMIT ? OFFSET ?",concat(args,String.valueOf(Math.min(limit,500)),String.valueOf(Math.max(offset,0))))){while(c.moveToNext())a.put(row(c));}
        return a;
    }
    private String[] concat(List<String> a,String... b){ArrayList<String> v=new ArrayList<>(a);Collections.addAll(v,b);return v.toArray(new String[0]);}
    synchronized JSONArray trashList(String mode,String query,int offset,int limit) throws Exception {
        purgeExpired();
        ArrayList<String> args=new ArrayList<>();String where="deleted_at>?";args.add(String.valueOf(System.currentTimeMillis()-TRASH_RETENTION_MS));
        if(mode.equals("in")||mode.equals("out")){where+=" AND mode=?";args.add(mode);}
        if(!query.isEmpty()){where+=" AND instr(sku,?)>0";args.add(query.toUpperCase(Locale.ROOT));}
        JSONArray rows=new JSONArray();
        try(Cursor c=getReadableDatabase().rawQuery("SELECT * FROM records WHERE "+where+" ORDER BY deleted_at DESC,id LIMIT ? OFFSET ?",concat(args,String.valueOf(Math.min(limit,500)),String.valueOf(Math.max(offset,0))))){while(c.moveToNext())rows.put(row(c));}
        return rows;
    }
    synchronized void trash(String id) throws Exception {
        if(cleanupPending())throw new IOException("请先完成记录清理");
        ContentValues values=new ContentValues();values.put("deleted_at",System.currentTimeMillis());
        SQLiteDatabase db=getWritableDatabase();db.beginTransaction();try{
            if(db.update("records",values,"id=? AND deleted_at IS NULL",new String[]{id})!=1)throw new IOException("记录已删除或不存在");
            db.execSQL("UPDATE records SET ocr_revision=ocr_revision+1 WHERE id=?",new Object[]{id});db.setTransactionSuccessful();
        }finally{db.endTransaction();}
    }
    synchronized void restore(String id) throws Exception {
        if(cleanupPending())throw new IOException("请先完成记录清理");
        // 保留原记录及同步状态；满 30 天的记录即使尚未执行磁盘清理也不可还原。
        ContentValues values=new ContentValues();values.putNull("deleted_at");
        if(getWritableDatabase().update("records",values,"id=? AND deleted_at>?",new String[]{id,String.valueOf(System.currentTimeMillis()-TRASH_RETENTION_MS)})!=1)throw new IOException("记录已还原、不存在或已过期");
    }
    synchronized void purgeExpired() throws IOException {
        SQLiteDatabase db=getWritableDatabase();String cutoff=String.valueOf(System.currentTimeMillis()-TRASH_RETENTION_MS);
        ArrayList<String> expired=new ArrayList<>();
        try(Cursor c=db.rawQuery("SELECT id FROM records WHERE deleted_at<=?",new String[]{cutoff})){
            while(c.moveToNext())expired.add(c.getString(0));
        }
        for(String id:expired){
                if(!id.matches("[0-9a-fA-F-]{36}"))throw new IOException("记录标识无效");
                // 先删图片，再删到期行。中断或文件删除失败时保留到期行供下次接续。
                for(String kind:new String[]{"product","label"}){File file=new File(images,id+"-"+kind+".jpg");if(file.exists()&&!file.delete())throw new IOException("回收站图片清理失败，请重试");}
                db.delete("records","id=? AND deleted_at<=?",new String[]{id,cutoff});
        }
    }
    synchronized JSONObject summary() throws JSONException {
        JSONObject o=new JSONObject();o.put("in",0);o.put("out",0);o.put("pending",0);o.put("total",0);o.put("analyzing",0);o.put("review",0);
        try(Cursor c=getReadableDatabase().rawQuery("SELECT ocr_status,count(*) FROM records WHERE deleted_at IS NULL GROUP BY ocr_status",null)){while(c.moveToNext()){if(c.getString(0).equals("pending"))o.put("analyzing",c.getInt(1));if(c.getString(0).equals("review"))o.put("review",c.getInt(1));}}
        try(Cursor c=getReadableDatabase().rawQuery("SELECT mode,count(*),sum(CASE WHEN synced=0 AND ocr_status='ready' THEN 1 ELSE 0 END) FROM records WHERE deleted_at IS NULL GROUP BY mode",null)){while(c.moveToNext()){o.put(c.getString(0),c.getInt(1));o.put("pending",o.getInt("pending")+c.getInt(2));o.put("total",o.getInt("total")+c.getInt(1));}}
        return o;
    }
    File image(String id,String kind) throws IOException {
        if(!id.matches("[0-9a-fA-F-]{36}")||(!kind.equals("product")&&!kind.equals("label")))throw new IOException("图片标识无效");
        File f=new File(images,id+"-"+kind+".jpg");if(!f.isFile())throw new FileNotFoundException("图片不存在");return f;
    }
    synchronized JSONObject payload(JSONObject record) throws Exception {
        try(Cursor c=getReadableDatabase().rawQuery("SELECT 1 FROM records WHERE id=? AND deleted_at IS NULL AND ocr_status='ready'",new String[]{record.getString("id")})){if(!c.moveToFirst())throw new IOException("记录尚未确认、已删除或不存在");}
        JSONObject result=new JSONObject(record.toString());result.put("revision",record.optInt("ocr_revision"));result.remove("synced");result.remove("deleted_at");
        for(String key:new String[]{"ocr_status","ocr_result","ocr_revision","recognition"})result.remove(key);
        for(String k:new String[]{"product","label"})result.put(k,android.util.Base64.encodeToString(Files.readAllBytes(image(record.getString("id"),k).toPath()),android.util.Base64.NO_WRAP));
        return result;
    }
    synchronized void acknowledge(String id) {ContentValues v=new ContentValues();v.put("synced",1);getWritableDatabase().update("records",v,"id=?",new String[]{id});}
    synchronized boolean acknowledge(String id,int revision) {ContentValues v=new ContentValues();v.put("synced",1);return getWritableDatabase().update("records",v,"id=? AND ocr_revision=? AND ocr_status='ready' AND deleted_at IS NULL",new String[]{id,String.valueOf(revision)})==1;}
    synchronized boolean cleanupPending(){try(Cursor c=getReadableDatabase().rawQuery("SELECT 1 FROM maintenance WHERE key='clear_pending'",null)){return c.moveToFirst();}}
    synchronized int clearAll(File draftDir,File exportDir) throws Exception {
        File root=images.getParentFile();
        if(!draftDir.getCanonicalFile().getParentFile().equals(root.getCanonicalFile())||!exportDir.getCanonicalFile().getParentFile().equals(root.getCanonicalFile()))throw new IOException("清理目录校验失败");
        SQLiteDatabase db=getWritableDatabase();int count;
        db.beginTransaction();
        try {
            try(Cursor c=db.rawQuery("SELECT count(*) FROM records",null)){c.moveToFirst();count=c.getInt(0);}
            db.execSQL("INSERT OR IGNORE INTO maintenance(key,value) VALUES('clear_pending',?)",new Object[]{count});
            db.delete("records",null,null);db.setTransactionSuccessful();
        } finally{db.endTransaction();}
        // 标记与删除记录在同一事务；清图中断后再次启动会接续，不允许新扫描混入。
        clearDirectory(root,images);clearDirectory(root,draftDir);clearDirectory(root,exportDir);
        return count;
    }
    synchronized void completeClear(){getWritableDatabase().delete("maintenance","key='clear_pending'",null);}
    private void clearDirectory(File root,File directory) throws IOException {
        if(!directory.exists())return;
        if(!directory.getCanonicalPath().startsWith(root.getCanonicalPath()+File.separator))throw new IOException("清理路径越界");
        File[] files=directory.listFiles();if(files==null)throw new IOException("无法读取清理目录，请重试");
        for(File file:files){if(file.isDirectory())clearDirectory(root,file);if(!file.delete())throw new IOException("部分图片或导出缓存未能删除，请重试清理");}
    }
}
