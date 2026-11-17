// 新装入口复用已发布的完整基础安装器；不重新携带或上传运行环境。
using System;
using System.IO;
using System.Net;
using System.Text;
using System.Reflection;
using System.Security.Cryptography;
using System.Diagnostics;
using System.Threading;
using System.Windows.Forms;
using System.Collections.Generic;
using System.Web.Script.Serialization;

static class BootstrapInstaller {
    static readonly JavaScriptSerializer Json=new JavaScriptSerializer {MaxJsonLength=16*1024*1024};
    static string Hash(string file) {using(var s=File.OpenRead(file))using(var h=SHA256.Create())return BitConverter.ToString(h.ComputeHash(s)).Replace("-","").ToLowerInvariant();}
    static string Prepare(string folder) {
        Directory.CreateDirectory(folder);Dictionary<string,object> meta;
        using(var s=Assembly.GetExecutingAssembly().GetManifestResourceStream("bootstrap.json"))using(var reader=new StreamReader(s,Encoding.UTF8))meta=Json.Deserialize<Dictionary<string,object>>(reader.ReadToEnd());
        string setup=Path.Combine(folder,"base-setup.exe");long size=Convert.ToInt64(meta["size"]);string digest=Convert.ToString(meta["sha256"]);
        if(!File.Exists(setup)||new FileInfo(setup).Length!=size||Hash(setup)!=digest) {
            ServicePointManager.SecurityProtocol=SecurityProtocolType.Tls12;
            using(var client=new WebClient())client.DownloadFile(Convert.ToString(meta["url"]),setup);
            if(new FileInfo(setup).Length!=size||Hash(setup)!=digest)throw new IOException("基础安装器下载校验失败，请重试。");
        }
        string envelope=Path.Combine(folder,"base-envelope.json");File.WriteAllText(envelope,Json.Serialize(meta["envelope"]),new UTF8Encoding(false));
        var settings=new {mode="install",root="",session=folder,parentPid=0,prepareComponents=true,installDriver=true,preferLatest=false,localEnvelope=envelope};
        File.WriteAllText(Path.Combine(folder,"session.json"),Json.Serialize(settings),new UTF8Encoding(false));
        return setup;
    }
    [STAThread] static int Main(string[] args) {
        bool verify=args.Length==2&&args[0]=="--prepare";
        string folder=verify?Path.GetFullPath(args[1]):Path.Combine(Path.GetTempPath(),"Framekeep","bootstrap-"+Guid.NewGuid().ToString("N"));
        try {
            if(verify) {Prepare(folder);return 0;}
            Application.EnableVisualStyles();
            using(var form=new Form {Text="SHIYING 数字资产安装",Width=440,Height=150,StartPosition=FormStartPosition.CenterScreen,FormBorderStyle=FormBorderStyle.FixedDialog,MaximizeBox=false,MinimizeBox=false}) {
                var label=new Label {Text="正在准备基础安装环境…\n安装完成后，软件将提示下载最新的小更新包。",AutoSize=true,Left=24,Top=20};form.Controls.Add(label);
                form.Controls.Add(new ProgressBar {Style=ProgressBarStyle.Marquee,Left=24,Top=75,Width=375,Height=12});
                form.Shown+=delegate {new Thread(()=>{
                    try {string setup=Prepare(folder);Process.Start(new ProcessStartInfo(setup,"--session \""+folder+"\"") {UseShellExecute=false,WorkingDirectory=folder,CreateNoWindow=true});form.BeginInvoke((Action)(()=>form.Close()));}
                    catch(Exception e) {form.BeginInvoke((Action)(()=>{MessageBox.Show(e.Message,"SHIYING 安装",MessageBoxButtons.OK,MessageBoxIcon.Error);form.Close();}));}
                }) {IsBackground=true}.Start();};
                Application.Run(form);
            }
            return 0;
        } catch(Exception error) {Directory.CreateDirectory(folder);File.WriteAllText(Path.Combine(folder,"bootstrap-error.txt"),error.ToString(),Encoding.UTF8);return 2;}
    }
}
