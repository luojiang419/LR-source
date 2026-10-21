using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("末帧选片")]
[assembly: System.Reflection.AssemblyProduct("末帧选片")]
[assembly: System.Reflection.AssemblyFileVersion("1.0.0.0")]

namespace FramekeepBootstrap {
    public static class Brand {
        public const string Name = "末帧选片";
        public const string Entry = "末帧选片.exe";
        public static string ExecutablePath(string root) {
            string current = System.IO.Path.Combine(root, Entry);
            return System.IO.File.Exists(current) ? current : System.IO.Path.Combine(root, "末帧选片.exe");
        }
    }
    // 安装器与独立更新器共用的原生视觉组件，不依赖安装或更新业务。
    public static class StudioTheme {
        public static Color Paper(bool dark) { return dark ? Color.FromArgb(29,31,29) : Color.FromArgb(251,250,247); }
        public static Color Ink(bool dark) { return dark ? Color.FromArgb(235,233,225) : Color.FromArgb(40,43,39); }
        public static Color Muted(bool dark) { return dark ? Color.FromArgb(158,161,150) : Color.FromArgb(124,128,116); }
        public static Color Accent(bool dark) { return dark ? Color.FromArgb(218,166,121) : Color.FromArgb(181,123,83); }
        public static Color Card(bool dark) { return dark ? Color.FromArgb(38,41,36) : Color.FromArgb(242,242,235); }
        public static Color Border(bool dark) { return dark ? Color.FromArgb(58,61,55) : Color.FromArgb(224,225,216); }
        public static Color Track(bool dark) { return dark ? Color.FromArgb(61,66,55) : Color.FromArgb(223,226,213); }
        public static GraphicsPath Round(RectangleF r, float radius) {
            var p = new GraphicsPath(); float d = Math.Min(radius * 2, Math.Min(r.Width, r.Height));
            p.AddArc(r.X, r.Y, d, d, 180, 90); p.AddArc(r.Right-d, r.Y, d, d, 270, 90);
            p.AddArc(r.Right-d, r.Bottom-d, d, d, 0, 90); p.AddArc(r.X, r.Bottom-d, d, d, 90, 90); p.CloseFigure(); return p;
        }
    }

    public enum ButtonGlyph { None, Close, Minimize, Sun, Moon }
    public sealed class SoftButton : Control, IButtonControl {
        public bool Primary, Dark;
        public ButtonGlyph Glyph;
        bool hovering, pressed;
        Bitmap background;
        Color cachedSurface, cachedPaper, cachedInk;
        ButtonGlyph cachedGlyph;
        public DialogResult DialogResult { get; set; }
        public SoftButton() {
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.Selectable | ControlStyles.StandardClick, true);
            Cursor = Cursors.Hand; TabStop = true; AccessibleRole = AccessibleRole.PushButton;
            Font = new Font("Microsoft YaHei UI", 10);
        }
        public void NotifyDefault(bool value) { Invalidate(); }
        public void PerformClick() { if (CanSelect) OnClick(EventArgs.Empty); }
        protected override AccessibleObject CreateAccessibilityInstance() { return new ButtonAccessibility(this); }
        sealed class ButtonAccessibility : ControlAccessibleObject {
            readonly SoftButton button;
            public ButtonAccessibility(SoftButton value) : base(value) { button=value; }
            public override string DefaultAction { get { return "点击"; } }
            public override void DoDefaultAction() { button.PerformClick(); }
        }
        protected override void OnClick(EventArgs e) {
            if (!Enabled) return;
            if (DialogResult != DialogResult.None && FindForm() != null) FindForm().DialogResult = DialogResult;
            base.OnClick(e);
        }
        protected override void OnMouseEnter(EventArgs e) { hovering = true; Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { hovering = false; pressed = false; Invalidate(); base.OnMouseLeave(e); }
        protected override void OnMouseDown(MouseEventArgs e) { if (Enabled && e.Button == MouseButtons.Left) { pressed = true; Focus(); Invalidate(); } base.OnMouseDown(e); }
        protected override void OnMouseUp(MouseEventArgs e) { pressed = false; Invalidate(); base.OnMouseUp(e); }
        protected override void OnMouseCaptureChanged(EventArgs e) { pressed = false; Invalidate(); base.OnMouseCaptureChanged(e); }
        protected override void OnEnabledChanged(EventArgs e) { pressed = false; Invalidate(); base.OnEnabledChanged(e); }
        protected override void OnTextChanged(EventArgs e) { base.OnTextChanged(e); Invalidate(); }
        protected override void OnGotFocus(EventArgs e) { Invalidate(); base.OnGotFocus(e); }
        protected override void OnLostFocus(EventArgs e) { pressed = false; Invalidate(); base.OnLostFocus(e); }
        protected override bool IsInputKey(Keys keyData) { return (keyData & Keys.KeyCode) == Keys.Space || (keyData & Keys.KeyCode) == Keys.Enter || base.IsInputKey(keyData); }
        protected override void OnKeyDown(KeyEventArgs e) {
            if (e.KeyCode == Keys.Space && Enabled) { pressed = true; Invalidate(); e.Handled = true; }
            if (e.KeyCode == Keys.Enter && Enabled) { PerformClick(); e.Handled = true; e.SuppressKeyPress = true; }
            base.OnKeyDown(e);
        }
        protected override void OnKeyUp(KeyEventArgs e) { if (e.KeyCode == Keys.Space && pressed) { pressed = false; PerformClick(); Invalidate(); e.Handled = true; } base.OnKeyUp(e); }
        protected override void OnPaintBackground(PaintEventArgs e) { e.Graphics.Clear(Parent == null ? BackColor : Parent.BackColor); }
        void CacheBackground(Color paper, Color surface, Color ink) {
            if (background != null && background.Size == ClientSize && cachedPaper == paper && cachedSurface == surface && cachedInk == ink && cachedGlyph == Glyph) return;
            if (background != null) background.Dispose();
            background = new Bitmap(Width, Height, System.Drawing.Imaging.PixelFormat.Format32bppPArgb);
            // 圆角先以 4 倍尺寸绘制再缩小，并用父背景预合成，避免原生主题白底和透明边缘白色杂边。
            using (var large = new Bitmap(Width * 4, Height * 4, System.Drawing.Imaging.PixelFormat.Format32bppPArgb)) {
                using (var g = Graphics.FromImage(large)) {
                    g.Clear(paper); g.ScaleTransform(4,4); g.SmoothingMode = SmoothingMode.AntiAlias;
                    float radius=Glyph==ButtonGlyph.None?Math.Min(12,Height*.27f):Math.Min(Width,Height)/2f-.5f;
                    using (var shape = StudioTheme.Round(new RectangleF(.5f,.5f,Width-1,Height-1),radius))
                    using (var fill = new SolidBrush(surface)) g.FillPath(fill,shape);
                    if(Glyph!=ButtonGlyph.None) {
                        float scale=Math.Min(Width,Height)/28f, cx=Width/2f, cy=Height/2f, r=3.5f*scale;
                        using(var pen=new Pen(ink,1.35f*scale) { StartCap=LineCap.Round,EndCap=LineCap.Round }) {
                            if(Glyph==ButtonGlyph.Close) { g.DrawLine(pen,cx-r,cy-r,cx+r,cy+r); g.DrawLine(pen,cx+r,cy-r,cx-r,cy+r); }
                            else if(Glyph==ButtonGlyph.Minimize) g.DrawLine(pen,cx-r,cy,cx+r,cy);
                            else if(Glyph==ButtonGlyph.Sun) {
                                g.DrawEllipse(pen,cx-2.5f*scale,cy-2.5f*scale,5*scale,5*scale);
                                for(int i=0;i<8;i++) { double angle=i*Math.PI/4; g.DrawLine(pen,cx+(float)Math.Cos(angle)*4.5f*scale,cy+(float)Math.Sin(angle)*4.5f*scale,cx+(float)Math.Cos(angle)*6.2f*scale,cy+(float)Math.Sin(angle)*6.2f*scale); }
                            } else {
                                using(var fill=new SolidBrush(ink)) g.FillEllipse(fill,cx-4.7f*scale,cy-4.7f*scale,9.4f*scale,9.4f*scale);
                                using(var cutout=new SolidBrush(surface)) g.FillEllipse(cutout,cx-1.9f*scale,cy-6.2f*scale,8.8f*scale,8.8f*scale);
                            }
                        }
                    }
                }
                using (var g = Graphics.FromImage(background)) {
                    g.CompositingMode = System.Drawing.Drawing2D.CompositingMode.SourceCopy;
                    g.InterpolationMode = InterpolationMode.HighQualityBicubic; g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                    // 镜像边界避免双三次采样读到图片外的透明黑，造成浅色背景亮边。
                    using(var attributes=new System.Drawing.Imaging.ImageAttributes()) {
                        attributes.SetWrapMode(WrapMode.TileFlipXY);
                        g.DrawImage(large,new Rectangle(0,0,Width,Height),0,0,large.Width,large.Height,GraphicsUnit.Pixel,attributes);
                    }
                }
            }
            cachedPaper=paper; cachedSurface=surface; cachedInk=ink; cachedGlyph=Glyph;
        }
        protected override void OnPaint(PaintEventArgs e) {
            if (Width < 2 || Height < 2) return;
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            Color surface = Primary ? (Dark ? Color.FromArgb(236, 232, 224) : Color.FromArgb(40, 42, 39)) : (Dark ? Color.FromArgb(43, 44, 41) : Color.FromArgb(242, 241, 236));
            if (hovering && Enabled) surface = Primary ? (Dark ? Color.White : Color.FromArgb(66, 67, 61)) : (Dark ? Color.FromArgb(55, 56, 51) : Color.FromArgb(233, 231, 224));
            if (pressed && Enabled) surface = Primary ? (Dark ? Color.FromArgb(218,214,205) : Color.FromArgb(27,30,26)) : (Dark ? Color.FromArgb(65,69,59) : Color.FromArgb(221,225,211));
            Color ink = Primary ? (Dark ? Color.FromArgb(35, 36, 33) : Color.White) : (Dark ? Color.FromArgb(217, 216, 207) : Color.FromArgb(83, 85, 78));
            if (!Enabled) ink = Color.FromArgb(146, 146, 136);
            CacheBackground(Parent == null ? BackColor : Parent.BackColor,surface,ink);
            e.Graphics.DrawImageUnscaled(background,0,0);
            if(Glyph==ButtonGlyph.None) TextRenderer.DrawText(e.Graphics, Text, Font, ClientRectangle, ink, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
            if (Focused && ShowFocusCues) using(var pen=new Pen(Dark?Color.FromArgb(190,157,114):Color.FromArgb(147,120,82)) { DashStyle=DashStyle.Dot })
                using(var path=StudioTheme.Round(new RectangleF(4,4,Width-9,Height-9),8)) e.Graphics.DrawPath(pen,path);
        }
        protected override void Dispose(bool disposing) { if(disposing && background!=null) background.Dispose(); base.Dispose(disposing); }
    }

}
