'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const photos = [], events = new Map(), history = [], messages = [];
  let current = -1, token = 0, connected = true, armed = false, lastEvent = null, sequence = 0;
  let autoTimer = null, wifiDelay = '60';
  const palettes = [['#c8b4a0','#987b67','#e6d5bc','#546956'],['#b0bdc6','#6f8795','#d6dfe2','#b88058'],['#bcaaa9','#926e77','#e2d4cc','#666e8e'],['#a8b9ad','#708d7f','#d7dfce','#a56f51']];
  function art(number) {
    const p = palettes[(number - 1) % palettes.length];
    return `<svg viewBox="0 0 960 540" role="img" aria-label="第 ${number} 张静物摄影示意图，并非相机实拍"><defs><linearGradient id="wall" x2="0" y2="1"><stop stop-color="${p[0]}"/><stop offset="1" stop-color="${p[2]}"/></linearGradient><radialGradient id="orb" cx="32%" cy="25%" r="75%"><stop stop-color="${p[2]}"/><stop offset=".45" stop-color="${p[0]}"/><stop offset="1" stop-color="${p[1]}"/></radialGradient><filter id="blur"><feGaussianBlur stdDeviation="14"/></filter></defs><rect width="960" height="540" fill="url(#wall)"/><path d="M0 385L960 330V540H0Z" fill="${p[1]}"/><path d="M710 0L450 400H560L860 0Z" fill="${p[2]}" opacity=".22"/><ellipse cx="490" cy="431" rx="230" ry="29" fill="#352f2b" opacity=".22" filter="url(#blur)"/><path d="M325 320L525 293L629 354L426 385Z" fill="${p[2]}"/><path d="M325 320V431L426 495V385Z" fill="${p[1]}"/><path d="M426 385L629 354V457L426 495Z" fill="${p[0]}"/><circle cx="${470 + (number % 3) * 14}" cy="248" r="101" fill="url(#orb)"/><path d="M686 397C676 316 708 270 733 241C726 313 753 361 735 414Z" fill="${p[3]}"/><path d="M711 402C665 356 657 328 664 304C700 325 728 365 729 408Z" fill="${p[3]}"/><path d="M673 396H759L746 476H686Z" fill="${p[2]}"/><text x="34" y="48" fill="#282b2d" opacity=".65" font-family="Segoe UI, sans-serif" font-size="13" letter-spacing="4">STUDIO STUDY / ${String(number).padStart(2, '0')}</text></svg><span class="screen-label">示意画面 · RAW + JPG 配对 · 非真实相机输入</span>`;
  }
  function log(text, type = '') {
    messages.unshift({text, type, time:new Date().toLocaleTimeString('zh-CN', {hour12:false})});
    messages.splice(8);
    $('log').replaceChildren(...messages.map(m => {
      const li = document.createElement('li'), time = document.createElement('time');
      li.className = m.type; time.textContent = m.time; li.append(time, document.createTextNode(m.text)); return li;
    }));
  }
  function status(text, warning = false) {
    $('status').textContent = text;
    $('status').classList.toggle('warning', warning);
  }
  function render() {
    const photo = photos[current];
    $('filename').textContent = photo ? photo.name + '.CR3' : '尚无照片';
    $('pairing').textContent = photo ? `+ ${photo.name}.JPG · 第 ${current + 1} / ${photos.length} 张` : '原图不读写';
    $('rating').textContent = photo?.rating ? '★ 1 星' : '未标星';
    $('rating').classList.toggle('rated', !!photo?.rating);
    $('selected').textContent = photos.filter(p => p.rating).length;
    $('queue').textContent = `待看 ${Math.max(0, photos.length - current - 1)} 张`;
    $('next').disabled = current >= photos.length - 1;
    $('undo').disabled = !history.length;
    $('retry').disabled = !lastEvent || !connected;
    $('export').disabled = !photos.some(p => p.rating);
    $('star').disabled = !connected || !armed || !photo;
    $('connection').textContent = connected ? '断开' : '重新连接';
    $('delay').disabled = $('transport').value === 'usb';
    $('led').className = 'led' + (connected ? (armed ? ' ready' : ' pending') : '');
    $('filmstrip').replaceChildren(...photos.map((p, i) => {
      const b = document.createElement('button'); b.className = 'thumb';
      b.setAttribute('aria-pressed', String(i === current));
      b.setAttribute('aria-label', `查看 ${p.name}${p.rating ? '，1 星' : '，未标星'}`);
      b.textContent = String(i + 1).padStart(3, '0');
      if (p.rating) {const star = document.createElement('span'); star.className = 'mini-star'; star.textContent = '★'; b.append(star);}
      b.addEventListener('click', () => show(i)); return b;
    }));
  }
  function rearm() {
    armed = false; const expected = ++token;
    status(connected ? (current < 0 ? '已连接，等待照片' : '正在绑定当前照片…') : '连接已断开，按键不可用', !connected);
    render();
    if (!connected || current < 0) return;
    setTimeout(() => {
      if (token !== expected || !connected) return;
      armed = true; status(`按钮就绪 · ${photos[current].name}`); render();
    }, 120);
  }
  function scheduleAuto() {
    clearTimeout(autoTimer); autoTimer = null;
    if ($('auto').checked && current >= 0 && current < photos.length - 1) {
      autoTimer = setTimeout(() => { if ($('auto').checked) show(current + 1); }, 1500);
    }
  }
  function show(i) {
    if (i < 0 || i >= photos.length) return;
    current = i; $('screen').innerHTML = art(i + 1); rearm(); scheduleAuto();
  }
  function receive(event) {
    // Simulated in-memory commit ledger. Production needs a single SQLite transaction.
    if (!connected) {log(`${event.id}：连接已断开，未补发到其他照片`, 'warning'); status('断线事件未执行；请核对后再按', true); return;}
    if (events.has(event.id)) {
      log(`${event.id}：命中成功事件，返回原 ACK；不重新写星`, 'success'); status('重发已确认；星级保持当前状态'); return;
    }
    if (!armed || event.token !== token || event.captureId !== photos[current]?.id) {
      log(`${event.id}：照片令牌已过期，拒绝；当前照片未被此事件修改`, 'warning');
      status('迟到事件已拒绝，请回看目标照片后再按', true); return;
    }
    const photo = photos[current];
    if (photo.rating !== 1) {history.push({id:photo.id, before:photo.rating}); photo.rating = 1;}
    events.set(event.id, {captureId:photo.id, rating:1});
    log(`${event.id}：${photo.name} → 1 星，模拟保存完成 → ACK`, 'success');
    status(`已确认 · ${photo.name} ★`); render();
  }
  $('capture').addEventListener('click', () => {
    const n = photos.length + 1;
    if (n > 100) {log('演示上限为 100 张；真实接片能力须另外验证', 'warning'); return;}
    photos.push({id:`demo-cap-${n}`, name:`DEMO_${String(n).padStart(4, '0')}`, rating:0});
    log(`模拟落盘就绪：DEMO_${String(n).padStart(4, '0')}，RAW/JPG 合并为一张`);
    if (current < 0) show(0); else {render(); if (!autoTimer) scheduleAuto();}
  });
  $('next').addEventListener('click', () => show(current + 1));
  $('auto').addEventListener('change', scheduleAuto);
  $('star').addEventListener('click', () => {
    if (!connected || !armed || current < 0) return;
    const event = {id:`demo-boot:${++sequence}`, token, captureId:photos[current].id};
    lastEvent = event;
    const delay = $('transport').value === 'usb' ? 25 : Number($('delay').value);
    log(`${event.id}：按下时锁定 ${photos[current].name}；模拟传输 ${delay}ms`);
    status('事件传输中，尚未确认保存…'); render();
    setTimeout(() => receive(event), delay);
  });
  $('retry').addEventListener('click', () => {if (lastEvent && connected) receive(lastEvent);});
  $('connection').addEventListener('click', () => {
    connected = !connected;
    log(connected ? '连接恢复，重新绑定；不会回放旧按键' : '已断开；当前照片令牌失效', connected ? '' : 'warning'); rearm();
  });
  $('transport').addEventListener('change', () => {
    $('delay').value = $('transport').value === 'usb' ? '25' : wifiDelay;
    log(`切换到 ${$('transport').value === 'usb' ? 'USB CDC' : 'Wi-Fi'}（模拟），旧照片令牌失效`); rearm();
  });
  $('delay').addEventListener('change', () => {if ($('transport').value === 'wifi') wifiDelay = $('delay').value;});
  $('undo').addEventListener('click', () => {
    const action = history.pop(); if (!action) return;
    const photo = photos.find(p => p.id === action.id); photo.rating = action.before;
    log(`已撤销 ${photo.name} 的标星；旧成功事件重发不会重新应用`);
    status(`已撤销 · ${photo.name}`); render();
  });
  $('export').addEventListener('click', () => {
    const selected = photos.filter(p => p.rating);
    const payload = {schema:'framekeep.hardware-demo', version:1, demo:true, notForLightroom:true,
      createdAt:new Date().toISOString(), note:'仅为内存模拟，不含真实照片。不能用于 LR 桥接。',
      captures:selected.map(p => ({id:p.id, relativePaths:[p.name+'.CR3', p.name+'.JPG'], rating:p.rating}))};
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], {type:'application/json'}));
    const a = document.createElement('a'); a.href = url; a.download = '末帧-硬件选片模拟.demo.json';
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    log(`下载模拟清单：${selected.length} 张，demo=true，不用于 LR`, 'success');
  });
  document.addEventListener('keydown', event => {
    if (event.code === 'Space' && !event.repeat && event.target === document.body) {
      event.preventDefault(); if (!$('star').disabled) $('star').click();
    }
  });
  render();
})();
