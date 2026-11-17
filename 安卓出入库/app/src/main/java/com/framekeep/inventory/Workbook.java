package com.framekeep.inventory;
import android.graphics.BitmapFactory;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.zip.*;
final class Workbook {
    static final String NS="http://schemas.openxmlformats.org/spreadsheetml/2006/main",REL="http://schemas.openxmlformats.org/officeDocument/2006/relationships",PKG="http://schemas.openxmlformats.org/package/2006/relationships";
    static String esc(String s){return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace("\"","&quot;").replaceAll("[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F]","");}
    static void part(ZipOutputStream zip,String name,String xml)throws IOException {zip.putNextEntry(new ZipEntry(name));zip.write(xml.getBytes(StandardCharsets.UTF_8));zip.closeEntry();}
    static void write(File target,InventoryStore store,String mode,String query) throws Exception {
        write(target,store,mode,query,ExportRange.all());
    }
    static void write(File target,InventoryStore store,String mode,String query,ExportRange range) throws Exception {
        File tmp=new File(target+".tmp");
        try(ZipOutputStream z=new ZipOutputStream(new BufferedOutputStream(new FileOutputStream(tmp)))) {
            part(z,"[Content_Types].xml","<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"><Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/><Default Extension=\"xml\" ContentType=\"application/xml\"/><Default Extension=\"jpg\" ContentType=\"image/jpeg\"/><Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/><Override PartName=\"/xl/worksheets/sheet1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/><Override PartName=\"/xl/styles.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml\"/><Override PartName=\"/xl/drawings/drawing1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.drawing+xml\"/></Types>");
            part(z,"_rels/.rels","<Relationships xmlns=\""+PKG+"\"><Relationship Id=\"rId1\" Type=\""+REL+"/officeDocument\" Target=\"xl/workbook.xml\"/></Relationships>");
            part(z,"xl/workbook.xml","<workbook xmlns=\""+NS+"\" xmlns:r=\""+REL+"\"><sheets><sheet name=\"出入库记录\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>");
            part(z,"xl/_rels/workbook.xml.rels","<Relationships xmlns=\""+PKG+"\"><Relationship Id=\"rId1\" Type=\""+REL+"/worksheet\" Target=\"worksheets/sheet1.xml\"/><Relationship Id=\"rId2\" Type=\""+REL+"/styles\" Target=\"styles.xml\"/></Relationships>");
            part(z,"xl/styles.xml","<styleSheet xmlns=\""+NS+"\"><fonts count=\"1\"><font><sz val=\"11\"/><name val=\"Microsoft YaHei\"/></font></fonts><fills count=\"2\"><fill><patternFill patternType=\"none\"/></fill><fill><patternFill patternType=\"gray125\"/></fill></fills><borders count=\"1\"><border/></borders><cellStyleXfs count=\"1\"><xf/></cellStyleXfs><cellXfs count=\"1\"><xf fontId=\"0\" fillId=\"0\" borderId=\"0\" xfId=\"0\" applyAlignment=\"1\"><alignment vertical=\"center\" wrapText=\"1\"/></xf></cellXfs><cellStyles count=\"1\"><cellStyle name=\"Normal\" xfId=\"0\" builtinId=\"0\"/></cellStyles></styleSheet>");
            StringBuilder sheet=new StringBuilder("<worksheet xmlns=\""+NS+"\" xmlns:r=\""+REL+"\"><sheetViews><sheetView workbookViewId=\"0\"><pane ySplit=\"1\" topLeftCell=\"A2\" state=\"frozen\"/></sheetView></sheetViews><cols><col min=\"1\" max=\"1\" width=\"8\" customWidth=\"1\"/><col min=\"2\" max=\"2\" width=\"27\" customWidth=\"1\"/><col min=\"3\" max=\"4\" width=\"26\" customWidth=\"1\"/><col min=\"5\" max=\"5\" width=\"10\" customWidth=\"1\"/><col min=\"6\" max=\"6\" width=\"29\" customWidth=\"1\"/></cols><sheetData>");
            StringBuilder drawing=new StringBuilder("<xdr:wsDr xmlns:xdr=\"http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing\" xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\" xmlns:r=\""+REL+"\">"),rels=new StringBuilder("<Relationships xmlns=\""+PKG+"\">");
            row(sheet,1,new String[]{"序号","款号","产品实拍图","SKU 标签图片","模式","出入库时间"},28);
            int index=0,image=0;
            while(true){JSONArray records=store.list(mode,query,index,500,false,range);if(records.length()==0)break;
                for(int i=0;i<records.length();i++) {
                    JSONObject r=records.getJSONObject(i);index++;row(sheet,index+1,new String[]{String.valueOf(index),r.optString("sku").isEmpty()?(r.optString("ocr_status").equals("pending")?"解析中":"存疑"):r.getString("sku"),"","",r.getString("mode").equals("in")?"入库":"出库",r.getString("at")},114);
                    for(int k=0;k<2;k++) {
                        File source=store.image(r.getString("id"),k==0?"product":"label");
                        BitmapFactory.Options bounds=new BitmapFactory.Options();bounds.inJustDecodeBounds=true;BitmapFactory.decodeFile(source.toString(),bounds);
                        if(bounds.outWidth<=0||bounds.outHeight<=0)throw new IOException("无法读取图片");
                        int w=bounds.outWidth,h=bounds.outHeight;float ratio=Math.min(164f/w,140f/h);long cx=Math.round(w*ratio)*9525L,cy=Math.round(h*ratio)*9525L;
                        // 仅调整表格显示尺寸，直接嵌入原文件，避免缩图和 JPEG 二次压缩。
                        image++;String name="image"+image+".jpg";z.putNextEntry(new ZipEntry("xl/media/"+name));java.nio.file.Files.copy(source.toPath(),z);z.closeEntry();
                        rels.append("<Relationship Id=\"rId").append(image).append("\" Type=\"").append(REL).append("/image\" Target=\"../media/").append(name).append("\"/>");
                        drawing.append("<xdr:oneCellAnchor><xdr:from><xdr:col>").append(k+2).append("</xdr:col><xdr:colOff>38100</xdr:colOff><xdr:row>").append(index).append("</xdr:row><xdr:rowOff>38100</xdr:rowOff></xdr:from><xdr:ext cx=\"").append(cx).append("\" cy=\"").append(cy).append("\"/><xdr:pic><xdr:nvPicPr><xdr:cNvPr id=\"").append(image).append("\" name=\"").append(name).append("\"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill><a:blip r:embed=\"rId").append(image).append("\"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill><xdr:spPr><a:prstGeom prst=\"rect\"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic><xdr:clientData/></xdr:oneCellAnchor>");
                    }
                }
                if(records.length()<500)break;
            }
            sheet.append("</sheetData><autoFilter ref=\"A1:F").append(index+1).append("\"/><drawing r:id=\"rId1\"/></worksheet>");
            part(z,"xl/worksheets/sheet1.xml",sheet.toString());part(z,"xl/worksheets/_rels/sheet1.xml.rels","<Relationships xmlns=\""+PKG+"\"><Relationship Id=\"rId1\" Type=\""+REL+"/drawing\" Target=\"../drawings/drawing1.xml\"/></Relationships>");
            part(z,"xl/drawings/drawing1.xml",drawing.append("</xdr:wsDr>").toString());part(z,"xl/drawings/_rels/drawing1.xml.rels",rels.append("</Relationships>").toString());
        } catch(Exception e){tmp.delete();throw e;}
        if(!tmp.renameTo(target))throw new IOException("导出文件保存失败");
    }
    static void row(StringBuilder s,int row,String[] values,int height){s.append("<row r=\"").append(row).append("\" ht=\"").append(height).append("\" customHeight=\"1\">");for(int i=0;i<values.length;i++)s.append("<c r=\"").append((char)('A'+i)).append(row).append("\" t=\"inlineStr\"><is><t xml:space=\"preserve\">").append(esc(values[i])).append("</t></is></c>");s.append("</row>");}
}
