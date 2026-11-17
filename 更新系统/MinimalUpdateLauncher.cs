// 最小离线更新入口：验证既有基线，复用本机受管更新环境。
using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using System.Collections;
using System.Diagnostics;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

static class MinimalUpdateLauncher {
    static readonly JavaScriptSerializer Json=new JavaScriptSerializer {MaxJsonLength=16*1024*1024};
    static Dictionary<string,object> Read(string path) {return Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(path,Encoding.UTF8));}
    static Dictionary<string,object> Map(object value) {return (Dictionary<string,object>)value;}
    static string Hash(string path) {using(var input=File.OpenRead(path))using(var sha=SHA256.Create())return BitConverter.ToString(sha.ComputeHash(input)).Replace("-","").ToLowerInvariant();}
    static string Under(string root,string relative) {
        if(Path.IsPathRooted(relative)||relative.Split('/','\\').Any(p=>p==".."||p=="."||p==""))throw new IOException("更新路径无效");
        string result=Path.GetFullPath(Path.Combine(root,relative.Replace('/',Path.DirectorySeparatorChar)));
        if(!result.StartsWith(Path.GetFullPath(root).TrimEnd('\\')+"\\",StringComparison.OrdinalIgnoreCase))throw new IOException("更新路径越界");
        for(string p=result;!String.Equals(p,Path.GetFullPath(root),StringComparison.OrdinalIgnoreCase);p=Path.GetDirectoryName(p))
            if((File.Exists(p)||Directory.Exists(p))&&(File.GetAttributes(p)&FileAttributes.ReparsePoint)!=0)throw new IOException("更新路径包含链接，请使用原安装目录");
        return result;
    }
    static void Check(string path,Dictionary<string,object> record) {
        if(File.Exists(path)) {
            long size=new FileInfo(path).Length;string digest=Hash(path);var entries=new List<Dictionary<string,object>> {record};
            if(record.ContainsKey("compatible"))foreach(object item in (IEnumerable)record["compatible"])entries.Add(Map(item));
            if(entries.Any(r=>size==Convert.ToInt64(r["size"])&&digest==Convert.ToString(r["sha256"])))return;
        }
        throw new IOException("程序基线不匹配或文件损坏："+Path.GetFileName(path)+"。请使用完整安装器修复后再更新。");
    }
    static void Write(string path,object data) {Directory.CreateDirectory(Path.GetDirectoryName(path));File.WriteAllText(path,Json.Serialize(data),new UTF8Encoding(false));}
    static string Prepare(string root,string session,Dictionary<string,object> meta,string bundle) {
        root=Path.GetFullPath(root);session=Path.GetFullPath(session);
        var identity=Read(Path.Combine(root,"update-config.json"));
        var baselines=Map(meta["baselines"]);string release=Convert.ToString(identity["releaseId"]);
        var versions=meta.ContainsKey("baselineVersions")?Map(meta["baselineVersions"]):null;
        string expected=versions!=null&&versions.ContainsKey(release)?Convert.ToString(versions[release]):Convert.ToString(meta["version"]);
        if(Convert.ToString(identity["version"])!=expected||!baselines.ContainsKey(release))throw new IOException("此最小包适用于 "+Convert.ToString(meta["baselineLabel"])+"，请确认选择的是原安装目录。");
        Check(Path.Combine(bundle,"SHIYING-Windows-x64.zip"),Map(meta["package"]));
        Check(Path.Combine(bundle,"envelope.json"),Map(meta["envelope"]));
        foreach(object item in (IEnumerable)baselines[release]) {var r=Map(item);Check(Under(root,Convert.ToString(r["path"])),r);}
        var worker=Read(Path.Combine(root,"update/worker-manifest.json"));
        if(Convert.ToInt32(worker["schema"])!=1)throw new IOException("现有更新环境清单无效");
        Directory.CreateDirectory(session);
        foreach(object item in (IEnumerable)worker["files"]) {
            var r=Map(item);string path=Convert.ToString(r["path"]);
            if(path!="update-worker.exe"&&!path.StartsWith("_internal/",StringComparison.Ordinal))throw new IOException("更新环境路径无效");
            string source=Under(Path.Combine(root,"update"),path),target=Under(session,path);Check(source,r);
            Directory.CreateDirectory(Path.GetDirectoryName(target));File.Copy(source,target,false);Check(target,r);
        }
        File.Copy(Path.Combine(root,"update/worker-manifest.json"),Path.Combine(session,"worker-manifest.json"),false);
        string gui=Path.Combine(session,"SHIYING 更新器.exe");File.Copy(Path.Combine(root,"update/SHIYING 更新器.exe"),gui,false);
        string data=Path.Combine(root,"data"),marker=Path.Combine(root,".framekeep-install.json");bool integrated=false;
        if(File.Exists(marker)) {
            var m=Read(marker);
            if(Convert.ToString(m["product"])!="framekeep"||!String.Equals(Path.GetFullPath(Convert.ToString(m["root"])),root,StringComparison.OrdinalIgnoreCase))throw new IOException("安装记录与所选目录不匹配");
            if(m.ContainsKey("dataPath"))data=Convert.ToString(m["dataPath"]);
            integrated=m.ContainsKey("integrated")&&Convert.ToBoolean(m["integrated"]);
        }
        Write(Path.Combine(session,"session.json"),new {mode="update",root,session,data,parentPid=0,enginePid=0,port=8769,integration=integrated,prepareComponents=false,preferLatest=false,localEnvelope=Path.Combine(bundle,"envelope.json"),localPackage=Path.Combine(bundle,"SHIYING-Windows-x64.zip")});
        return gui;
    }
    [STAThread] static int Main(string[] args) {
        bool prepare=args.Length==3&&args[0]=="--prepare";
        try {
            Application.EnableVisualStyles();string bundle=Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            Dictionary<string,object> meta;
            using(var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("minimal.json"))using(var reader=new StreamReader(stream,Encoding.UTF8))meta=Json.Deserialize<Dictionary<string,object>>(reader.ReadToEnd());
            string root=prepare?args[1]:"";
            if(!prepare) {
                using(var key=Registry.CurrentUser.OpenSubKey("Software\\Framekeep"))if(key!=null)root=Convert.ToString(key.GetValue("InstallLocation",""));
                using(var dialog=new FolderBrowserDialog {Description="选择客户机现有的 SHIYING 安装目录",SelectedPath=root,ShowNewFolderButton=false}) {if(dialog.ShowDialog()!=DialogResult.OK)return 1;root=dialog.SelectedPath;}
                if(MessageBox.Show("请先保存当前操作。更新将保留照片、资料和已有模型，然后重新启动软件。","SHIYING 最小更新",MessageBoxButtons.OKCancel,MessageBoxIcon.Information)!=DialogResult.OK)return 1;
            }
            string session=prepare?args[2]:Path.Combine(Path.GetTempPath(),"Framekeep","minimal-"+Convert.ToString(meta["releaseId"])+"-"+Guid.NewGuid().ToString("N"));
            string gui=Prepare(root,session,meta,bundle);
            if(!prepare)Process.Start(new ProcessStartInfo(gui,"--session \""+session+"\"") {UseShellExecute=false,WorkingDirectory=session,CreateNoWindow=true});
            return 0;
        } catch(Exception error) {
            if(prepare)File.WriteAllText(args[2]+".error.txt",error.ToString(),Encoding.UTF8);
            else MessageBox.Show(error.Message,"SHIYING 最小更新",MessageBoxButtons.OK,MessageBoxIcon.Error);
            return 2;
        }
    }
}
