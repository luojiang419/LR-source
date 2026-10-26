// 原生窗口与按钮复用 VoxCPM SharedTheme，安装及更新事务交给独立冻结进程。
using System;
using System.IO;
using System.Text;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Diagnostics;
using System.Reflection;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace FramekeepBootstrap {
    static class Program {
        public static readonly JavaScriptSerializer Json = new JavaScriptSerializer {MaxJsonLength=16*1024*1024};
        public static string Own { get { return Assembly.GetExecutingAssembly().Location; } }
        public static string Quote(string value) { return "\"" + value.Replace("\"", "") .TrimEnd('\\') + "\""; }
        public static Dictionary<string,object> Read(string file) { return Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(file,Encoding.UTF8)); }
        public static void Write(string file,object value) {
            string temp=file+".native-tmp"; File.WriteAllText(temp,Json.Serialize(value),new UTF8Encoding(false));
            if(File.Exists(file)) File.Replace(temp,file,null); else File.Move(temp,file);
        }
        [STAThread]
        static void Main(string[] args) {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            try {
                if(args.Length==2 && args[0]=="--render") {
                    string output=Path.GetFullPath(args[1]);Directory.CreateDirectory(output);
                    string fixture=Path.Combine(output,"preview-session");Directory.CreateDirectory(fixture);
#if UNINSTALLER
                    Write(Path.Combine(fixture,"session.json"),new {mode="uninstall",root=@"D:\Program Files\Framekeep",session=fixture,parentPid=0});
#else
                    Write(Path.Combine(fixture,"session.json"),new {mode="install",root=@"D:\Program Files\Framekeep",session=fixture,parentPid=0});
#endif
                    using(var form=new UpdateForm(fixture)) {form.Show();Application.DoEvents();
                        form.ExportPreview(Path.Combine(output,"installer-light.png"),false,0);
                        form.ExportPreview(Path.Combine(output,"installer-light-frame24.png"),false,24);
                        form.ExportPreview(Path.Combine(output,"installer-dark.png"),true,24);form.Close();}
                    return;
                }
                string session;
                if(args.Length==2 && args[0]=="--session") session=Path.GetFullPath(args[1]);
                else {
#if INSTALLER
                    session=Path.Combine(Path.GetTempPath(),"Framekeep","updates",Guid.NewGuid().ToString("N"));
                    Directory.CreateDirectory(session);
                    var setup=new Dictionary<string,object>{{"mode","install"},{"root",""},{"session",session},{"parentPid",0},{"prepareComponents",true},{"installDriver",true}};
#if COMPLETE
                    string bundle=Path.GetDirectoryName(Own);
                    setup["localEnvelope"]=Path.Combine(bundle,"envelope.json");setup["localPackage"]=Path.Combine(bundle,"Framekeep-Windows-x64.zip");setup["componentSeed"]=Path.Combine(bundle,"components");
                    if(!File.Exists(Convert.ToString(setup["localEnvelope"])) || !File.Exists(Convert.ToString(setup["localPackage"])) || !Directory.Exists(Convert.ToString(setup["componentSeed"])))throw new IOException("请先完整解压安装包，并保留 components、软件包和安装器在同一目录。");
#endif
                    Write(Path.Combine(session,"session.json"),setup);
#elif UNINSTALLER
                    string root=Directory.GetParent(Path.GetDirectoryName(Own)).FullName;
                    var installed=Read(Path.Combine(root,".framekeep-install.json"));
                    if(Convert.ToString(installed["product"])!="framekeep" || !String.Equals(Path.GetFullPath(Convert.ToString(installed["root"])),root,StringComparison.OrdinalIgnoreCase))throw new IOException("安装记录与卸载程序位置不匹配。");
                    session=Path.Combine(Path.GetTempPath(),"Framekeep","uninstall",Guid.NewGuid().ToString("N"));Directory.CreateDirectory(session);
                    Write(Path.Combine(session,"session.json"),new {mode="uninstall",root,session,parentPid=0});
                    string clone=Path.Combine(session,"卸载末帧选片.exe");File.Copy(Own,clone);
                    Process.Start(new ProcessStartInfo(clone,"--session "+Quote(session)) {UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=session});
                    return;
#else
                    throw new IOException("请从末帧选片的检查更新入口启动更新器。");
#endif
                }
#if INSTALLER || UNINSTALLER
                using(var input=Assembly.GetExecutingAssembly().GetManifestResourceStream("worker.exe"))
                using(var output=File.Create(Path.Combine(session,"update-worker.exe"))) input.CopyTo(output);
#endif
                Application.Run(new UpdateForm(session));
            } catch(Exception error) { MessageBox.Show(error.Message,"末帧选片 · 安装与更新",MessageBoxButtons.OK,MessageBoxIcon.Error); }
        }
    }

    sealed class UpdateForm : Form {
        readonly string session;
        readonly bool install;
        readonly bool uninstall;
        readonly Dictionary<string,object> settings;
        readonly SoftButton start=new SoftButton { Primary=true }, secondary=new SoftButton(), browse=new SoftButton();
        readonly SoftButton theme=new SoftButton { Glyph=ButtonGlyph.Moon }, minimize=new SoftButton { Glyph=ButtonGlyph.Minimize }, close=new SoftButton { Glyph=ButtonGlyph.Close };
        readonly Label message=new Label(), detail=new Label();
        readonly TextBox root=new TextBox(), notes=new TextBox { Multiline=true,ReadOnly=true,BorderStyle=BorderStyle.None,ScrollBars=ScrollBars.Vertical };
        readonly Timer animation=new Timer { Interval=83 };
        readonly ToolTip tips=new ToolTip();
        readonly object logLock=new object();
        StreamWriter log;
        Image strip;
        int frame,progress;
        bool dark,working,finished,failed;
        Process worker;
        string heading;

        public UpdateForm(string folder) {
            session=folder; settings=Program.Read(Path.Combine(folder,"session.json"));
            install=Convert.ToString(settings["mode"])=="install";
            uninstall=Convert.ToString(settings["mode"])=="uninstall";
            Text="末帧选片 · "+(uninstall?"卸载程序":install?"安装程序":"独立更新器");
            ClientSize=new Size(720,560); AutoScaleMode=AutoScaleMode.None;
            StartPosition=FormStartPosition.CenterScreen; FormBorderStyle=FormBorderStyle.None;
            DoubleBuffered=true; Font=new Font("Microsoft YaHei UI",10);
            try { Icon=Icon.ExtractAssociatedIcon(Program.Own); } catch {}
            using(var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("animation.png")) if(stream!=null) using(var image=Image.FromStream(stream)) strip=new Bitmap(image);
            heading=uninstall?"告别应用，保留珍贵。":install?"让每一帧，各就其位。":"为下一次拍摄，准备就绪。";
            root.Text=Convert.ToString(settings["root"]);root.AccessibleName="安装目录";root.BorderStyle=BorderStyle.None;
            browse.Text="选择目录";browse.AccessibleName="选择安装目录";
            browse.Click+=delegate { using(var picker=new FolderBrowserDialog { Description="选择末帧选片的独立安装目录",SelectedPath=root.Text }) if(picker.ShowDialog(this)==DialogResult.OK) root.Text=picker.SelectedPath; };
            notes.Text=uninstall?"移除程序、桌面和开始菜单入口及系统应用注册。\r\n照片、评级、设置及模型资料保留。卸载前请保存并正常关闭末帧选片。":settings.ContainsKey("notes")?Convert.ToString(settings["notes"]):"注册到 Windows 已安装的应用，并创建桌面与开始菜单入口。\r\n优先选择空间充足的 D、E 等分区；无合适分区时使用无需提权的用户目录。";
            notes.AccessibleName="安装和更新说明";
            message.Text=uninstall?"卸载应用，保留照片与资料。":install?"从魔搭获取最新版末帧选片":"新版已准备好，请先保存当前任务";message.AccessibleName="安装更新状态";
            detail.Text=uninstall?"只删除登记过且未被修改的程序文件。":install?"安装时准备全部识别模型及依赖；鼠标驱动可能需要 Windows 授权与重启。":"确认后自动下载，保存成功且任务空闲后才退出工作台。";
            start.Text=uninstall?"卸载应用  →":install?"开始安装  →":"现在更新  →";start.AccessibleName=uninstall?"卸载应用":install?"开始安装":"现在更新";
            secondary.Text=install||uninstall?"查看日志":"下次启动更新";
            start.Click+=delegate {
                if(finished&&!failed) Close();
                else if(finished&&failed&&install) {
                    finished=failed=false;root.Visible=browse.Visible=notes.Visible=true;message.Visible=false;
                    start.Text="开始安装  →";heading="让每一帧，各就其位。";Invalidate();
                } else BeginWork();
            };
            secondary.Click+=delegate { if(!working && !install && !uninstall && !finished) Close(); else Process.Start(new ProcessStartInfo("explorer.exe",Program.Quote(session)) {UseShellExecute=true}); };
            theme.Text=theme.AccessibleName="切换主题";minimize.Text=minimize.AccessibleName="最小化";close.Text=close.AccessibleName="关闭";
            theme.Click+=delegate { dark=!dark;ApplyTheme(); };
            minimize.Click+=delegate { WindowState=FormWindowState.Minimized; };
            close.Click+=delegate { Close(); };
            start.SetBounds(464,496,216,44);secondary.SetBounds(40,496,170,44);
            theme.SetBounds(582,26,28,28);minimize.SetBounds(619,26,28,28);close.SetBounds(656,26,28,28);
            root.SetBounds(64,291,445,28);browse.SetBounds(526,284,128,36);
            notes.SetBounds(64,333,590,65);message.SetBounds(64,277,590,45);detail.SetBounds(64,387,590,26);
            message.AutoEllipsis=detail.AutoEllipsis=true;
            message.Visible=!install; root.Visible=browse.Visible=install;
            Controls.AddRange(new Control[]{start,secondary,browse,root,notes,message,detail,theme,minimize,close});
            animation.Tick+=delegate { frame=(frame+1)%48;Invalidate(new Rectangle(440,77,240,180)); };animation.Start();
            FormClosing+=delegate(object sender,FormClosingEventArgs e) { if(working)e.Cancel=true; };
            Shown+=delegate { if(install&&String.IsNullOrWhiteSpace(root.Text))PlanDirectory();else if(settings.ContainsKey("autoStart") && (bool)settings["autoStart"]) BeginWork(); };
            ApplyTheme();
        }

        void PlanDirectory() {
            start.Enabled=browse.Enabled=false;root.ReadOnly=true;detail.Text="正在选择空间充足且可写的安装位置…";
            System.Threading.ThreadPool.QueueUserWorkItem(delegate {
                string selected=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","Framekeep");
                string reason="使用无需管理员权限的用户安装目录。";
                try {
                    string output=Path.Combine(session,"install-plan.json");
                    var info=new ProcessStartInfo(Path.Combine(session,"update-worker.exe"),"--install-plan "+Program.Quote(output)) {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
                    using(var process=Process.Start(info)) {string text=process.StandardOutput.ReadToEnd();string error=process.StandardError.ReadToEnd();process.WaitForExit();if(process.ExitCode!=0)throw new IOException(text+error);}
                    var plan=Program.Read(output);selected=Convert.ToString(plan["root"]);reason=Convert.ToString(plan["reason"]);
                } catch(Exception error) {reason="请确认目录与可用空间："+error.Message;}
                if(!IsDisposed)BeginInvoke((Action)(()=>{root.Text=selected;detail.Text=reason;tips.SetToolTip(detail,reason);start.Enabled=browse.Enabled=true;root.ReadOnly=false;if(settings.ContainsKey("autoStart")&&(bool)settings["autoStart"])BeginWork();}));
            });
        }

        void ApplyTheme() {
            BackColor=StudioTheme.Paper(dark);ForeColor=StudioTheme.Ink(dark);
            foreach(var control in new Control[]{notes,message,detail,root}) {control.BackColor=StudioTheme.Card(dark);control.ForeColor=StudioTheme.Ink(dark);}
            notes.ForeColor=detail.ForeColor=StudioTheme.Muted(dark);
            foreach(var button in new[]{start,secondary,browse,theme,minimize,close}) {button.Dark=dark;button.BackColor=BackColor;button.Invalidate();}
            theme.Glyph=dark?ButtonGlyph.Sun:ButtonGlyph.Moon;Invalidate();
        }
        void TextAt(Graphics g,string text,float size,Color color,RectangleF rect,bool bold=false) {
            using(var font=new Font("Microsoft YaHei UI",size,bold?FontStyle.Bold:FontStyle.Regular,GraphicsUnit.Pixel))
            using(var brush=new SolidBrush(color)) g.DrawString(text,font,brush,rect);
        }
        protected override void OnPaint(PaintEventArgs e) {
            base.OnPaint(e);var g=e.Graphics;g.SmoothingMode=SmoothingMode.AntiAlias;
            using(var shape=StudioTheme.Round(new RectangleF(.5f,.5f,719,559),20)) using(var pen=new Pen(StudioTheme.Border(dark)))g.DrawPath(pen,shape);
            TextAt(g,"F.",38,StudioTheme.Accent(dark),new RectangleF(36,22,55,52),true);
            TextAt(g,"末帧选片",19,ForeColor,new RectangleF(94,29,190,28),true);
            TextAt(g,uninstall?"卸载程序 / FRAMEKEEP":install?"安装程序 / FRAMEKEEP":"独立更新器 / FRAMEKEEP",10,StudioTheme.Muted(dark),new RectangleF(95,58,300,20));
            TextAt(g,heading,28,ForeColor,new RectangleF(40,119,420,80),true);
            TextAt(g,"浏览照片 · 五星精选 · 资产归档",13,StudioTheme.Muted(dark),new RectangleF(42,205,396,24));
            if(strip!=null)g.DrawImage(strip,new Rectangle(440,77,240,180),new Rectangle(0,frame*180,240,180),GraphicsUnit.Pixel);
            using(var shape=StudioTheme.Round(new RectangleF(42,260,636,174),18)) using(var brush=new SolidBrush(StudioTheme.Card(dark)))g.FillPath(brush,shape);
            if(!working&&!finished&&install)TextAt(g,"安装到",11,StudioTheme.Muted(dark),new RectangleF(64,267,120,20));
            if(working||finished) {
                using(var shape=StudioTheme.Round(new RectangleF(64,416,590,5),2))using(var brush=new SolidBrush(StudioTheme.Track(dark)))g.FillPath(brush,shape);
                if(progress>0)using(var shape=StudioTheme.Round(new RectangleF(64,416,590*progress/100f,5),2))using(var brush=new SolidBrush(StudioTheme.Accent(dark)))g.FillPath(brush,shape);
            }
            TextAt(g,working?(uninstall?"检查运行状态  →  移除程序  →  清理应用注册":"下载校验  →  安装文件  →  启动验证"):finished?(failed?"未完成 · 请查看错误与日志":uninstall?"已卸载 · 照片与资料保留":"已完成 · 工作台已打开"):uninstall?"按安装清单移除应用 · 保留用户资料":"当前用户安装 · 桌面与开始菜单 · 支持卸载",11,StudioTheme.Muted(dark),new RectangleF(44,457,630,25));
        }
        protected override void OnMouseDown(MouseEventArgs e) {
            base.OnMouseDown(e);if(e.Button==MouseButtons.Left && e.Y<90){ReleaseCapture();SendMessage(Handle,0xA1,(IntPtr)2,IntPtr.Zero);}
        }
        [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool ReleaseCapture();
        [System.Runtime.InteropServices.DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr h,int m,IntPtr w,IntPtr l);
        void BeginWork() {
            if(working)return;
            try {
                if(install) {settings["root"]=Path.GetFullPath(root.Text.Trim().Trim('"'));Program.Write(Path.Combine(session,"session.json"),settings);}
                foreach(string name in new[]{"status.json","cancel.json","ready.json"})if(File.Exists(Path.Combine(session,name)))File.Delete(Path.Combine(session,name));
                working=true;finished=failed=false;progress=0;
                start.Enabled=close.Enabled=browse.Enabled=false;root.Visible=browse.Visible=notes.Visible=false;message.Visible=true;
                secondary.Text="查看日志";start.Text="正在处理…";message.Text=uninstall?"正在检查安装记录…":"正在准备独立更新环境…";detail.Text=uninstall?"程序和快捷方式将移除，照片及评级资料保留。":"下载校验完成后，自动安装并启动验证。";
                log=new StreamWriter(new FileStream(Path.Combine(session,"updater.log"),FileMode.Append,FileAccess.Write,FileShare.ReadWrite),new UTF8Encoding(false)){AutoFlush=true};
                var info=new ProcessStartInfo(Path.Combine(session,"update-worker.exe"),Program.Quote(session)) {
                    WorkingDirectory=session,UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,
                    StandardOutputEncoding=Encoding.UTF8,StandardErrorEncoding=Encoding.UTF8 };
                worker=new Process {StartInfo=info,EnableRaisingEvents=true};
                worker.OutputDataReceived+=delegate(object sender,DataReceivedEventArgs e){Receive(e.Data,true);};
                worker.ErrorDataReceived+=delegate(object sender,DataReceivedEventArgs e){Receive(e.Data,false);};
                worker.Exited+=delegate {worker.WaitForExit();if(!IsDisposed)BeginInvoke((Action)(()=>Finish(worker.ExitCode)));};
                worker.Start();worker.BeginOutputReadLine();worker.BeginErrorReadLine();Invalidate();
            } catch(Exception error) {message.Text=error.Message;Finish(1);}
        }
        public void ExportPreview(string path,bool value,int animationFrame) {
            animation.Stop();dark=value;frame=animationFrame;ApplyTheme();Application.DoEvents();
            using(var bitmap=new Bitmap(Width,Height)){DrawToBitmap(bitmap,ClientRectangle);bitmap.Save(path,System.Drawing.Imaging.ImageFormat.Png);}
        }
        void Receive(string line,bool parse) {
            if(line==null)return;lock(logLock)if(log!=null)log.WriteLine(line);
            if(!parse)return;
            try {var data=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(line);
                BeginInvoke((Action)(()=>{message.Text=Convert.ToString(data["message"]);tips.SetToolTip(message,message.Text);
                    progress=Convert.ToInt32(data["progress"]);Invalidate();}));} catch{}
        }
        void Finish(int code) {
            working=false;finished=true;failed=code!=0;start.Enabled=close.Enabled=true;
            lock(logLock)if(log!=null){log.Dispose();log=null;}
            start.Text=failed?"重试":"完成";start.AccessibleName=start.Text;
            heading=failed?"稍作停留，安心重试。":uninstall?"应用已离开，资料仍在。":"精选的下一程，现在开始。";
            detail.Text=failed?"原有资料保持，可查看日志后重试。":uninstall?"照片、评级、设置及模型资料已保留。":"软件与界面验证通过，可以开始选片。";
            if(!failed){progress=100;message.Text=uninstall?"已卸载末帧选片。":"已完成，末帧选片已启动。";}
            if(!failed&&install)try{var status=Program.Read(Path.Combine(session,"status.json"));var components=status["components"] as Dictionary<string,object>;if(components!=null){detail.Text="全部内置模型与依赖已准备完成，运行即可识别。";var driver=components["driver"] as Dictionary<string,object>;if(driver!=null&&driver.ContainsKey("restart_required")&&(bool)driver["restart_required"])message.Text="模型已就绪；鼠标驱动首次安装后可能需要重启电脑。";}}catch{}
            else try{var status=Program.Read(Path.Combine(session,"status.json"));message.Text=Convert.ToString(status["message"]);}catch{}
            AccessibleDescription=message.Text;tips.SetToolTip(message,message.Text);Invalidate();
        }
        protected override void Dispose(bool disposing) {
            if(disposing){animation.Stop();animation.Dispose();tips.Dispose();if(strip!=null)strip.Dispose();if(worker!=null)worker.Dispose();}
            base.Dispose(disposing);
        }
    }
}
