// life_lens 问相册(chat)共用逻辑 — vanilla JS,无框架。
//
// 被两个页面加载:
//   - index.html(桌面 5-tab 完整页):chat.js 必须在 app.js **之前**加载,
//     app.js 依赖这里定义的 api / escapeHtml / formatCapturedAt / refreshChatProviders 等全局
//   - chat.html(移动端问相册页,LAN gate 给局域网设备的入口):只加载本文件
//
// 抽离原则:聊天链路(SSE 循环 / markdown / photo viewer / id 前缀补全)单一实现,
// 桌面/移动共用,修 bug 不会两边漂移。

const api = (path, opts = {}) => fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
}).then(async r => {
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
});

function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// 照片拍摄时间(captured_at_local,相机原始 wall clock,通常无时区)
// 不能用 Date 解析(浏览器会按本地时区"假设" + 二次转换会错)— 纯字符串美化:
// "2024-08-15T19:23:01" → "2024-08-15 19:23",保留秒以上信息只切到分钟
function formatCapturedAt(s) {
    if (!s) return '';
    const m = String(s).match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
    return m ? `${m[1]} ${m[2]}` : s;
}

// ---- Photo Viewer modal(chat / Browse / 未引用区点缩略图共用)----
// 设计要点(plan):
//   - 主图 src = /api/thumb/{id}(1024px JPEG),不是原图 — HEIC 浏览器不能直接渲染,
//     原图几十 MB 加载慢。三重提示用户右键保存的是缩略图(title + 灰字 note + 按钮分流)
//   - 两个原图按钮共用 /api/original/{id} 同一文件源,区别只在 ?download=1 头部
//   - "完整详情 →" 跳 Browse + showDetail(仅桌面页;移动端 chat 页没有 browse tab,隐藏)
async function openViewer(id) {
    const v = document.getElementById('photo-viewer');
    const img = document.getElementById('viewer-img');
    const cap = v.querySelector('.viewer-caption');
    const desc = v.querySelector('.viewer-desc');
    const dlLink = document.getElementById('viewer-download');
    const detailBtn = document.getElementById('viewer-detail');

    img.src = `/api/thumb/${encodeURIComponent(id)}`;
    img.onerror = () => { img.style.background = '#fee'; };
    dlLink.href = `/api/original/${encodeURIComponent(id)}?download=1`;
    cap.textContent = '加载中……';
    desc.textContent = '';
    v.classList.remove('hidden');

    // 异步拉详情填 caption / desc
    try {
        const rec = await api('/photo/' + encodeURIComponent(id));
        const time = formatCapturedAt((rec.exif && (rec.exif.captured_at_local || rec.exif.captured_at_utc)) || '') || '(时间未知)';
        const place = (rec.derived && rec.derived.location_bucket
                       && (rec.derived.location_bucket.formatted_address
                           || rec.derived.location_bucket.place_name
                           || rec.derived.location_bucket.city)) || '';
        cap.textContent = place ? `${time} · ${place}` : time;
        const d = (rec.vision && rec.vision.description) || '';
        desc.textContent = d.length > 220 ? d.slice(0, 220) + '……' : d;
    } catch (e) {
        cap.textContent = `(加载详情失败: ${e.message})`;
    }

    // "完整详情 →" 依赖桌面页的 browse tab + showDetail;移动端 chat 页没有,隐藏按钮
    const browseTabBtn = document.querySelector('.tab[data-page="browse"]');
    if (browseTabBtn) {
        detailBtn.style.display = '';
        detailBtn.onclick = () => {
            closeViewer();
            browseTabBtn.click();
            setTimeout(() => showDetail(id), 200);
        };
    } else {
        detailBtn.style.display = 'none';
    }
}
function closeViewer() {
    const v = document.getElementById('photo-viewer');
    v.classList.add('hidden');
    document.getElementById('viewer-img').src = '';   // 释放
}
(function bindViewer() {
    // 脚本在 body 末尾加载,DOM 已就绪,直接绑定
    const v = document.getElementById('photo-viewer');
    if (!v) return;
    v.querySelector('.viewer-backdrop').onclick = closeViewer;
    document.getElementById('viewer-close').onclick = closeViewer;
})();
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        const v = document.getElementById('photo-viewer');
        if (v && !v.classList.contains('hidden')) closeViewer();
    }
});

// 回答下方追加 LLM 没引用但 search 召回的其他候选,默认折叠
function appendUncitedPhotos(parentDiv, photoIds, favoriteIds) {
    const wrap = document.createElement('details');
    wrap.className = 'chat-uncited';
    const summary = document.createElement('summary');
    summary.textContent = `另外 ${photoIds.length} 张相关候选(点击展开)`;
    wrap.appendChild(summary);
    const grid = document.createElement('div');
    grid.className = 'chat-photos';
    photoIds.forEach(id => {
        const w = document.createElement('span');
        w.className = 'chat-photo-wrap';
        w.title = id;
        const img = document.createElement('img');
        img.loading = 'lazy';
        let retried = false;
        img.onerror = () => {
            if (!retried) {
                retried = true;
                setTimeout(() => { img.src = `/api/thumb/${id}?_=${Date.now()}`; }, 300);
                return;
            }
            w.classList.add('missing');
            w.title = `图片缺失: ${id}`;
            w.textContent = '⚠ ' + id.slice(0, 8);
            img.remove();
        };
        img.onclick = () => openViewer(id);
        // src 只在 details 第一次展开时设置(避免一次性下载几十张缩略图)
        w.appendChild(img);
        _addFavStar(w, favoriteIds && favoriteIds.has(id));
        grid.appendChild(w);
    });
    wrap.appendChild(grid);
    // 第一次展开时再加载图片(lazy)
    wrap.addEventListener('toggle', () => {
        if (wrap.open) {
            grid.querySelectorAll('img').forEach(img => {
                if (!img.src) {
                    const wrapEl = img.closest('.chat-photo-wrap');
                    img.src = `/api/thumb/${wrapEl.title}`;
                }
            });
        }
    }, { once: true });
    parentDiv.appendChild(wrap);
}

// chat 工具调用 trace 框(让用户/调试者一眼看到 Round 1 LLM 选了啥工具 + args + 命中数)
function renderTrace(action, args, rationale, summary) {
    const escapeHtml = (s) => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const argsStr = Object.keys(args).length ? JSON.stringify(args, null, 0) : '{}';
    return `<div class="chat-trace-head">
                <span class="chat-trace-tool">${escapeHtml(action)}</span>
                <span class="chat-trace-args"><code>${escapeHtml(argsStr)}</code></span>
                <span class="chat-trace-summary">${escapeHtml(summary)}</span>
            </div>
            ${rationale ? `<div class="chat-trace-rationale">${escapeHtml(rationale)}</div>` : ''}`;
}

// ---- Chat ----
const chatLog = document.getElementById('chat-log');
const chatForm = document.getElementById('chat-form');
const chatInput = document.getElementById('chat-input');
const chatSend = document.getElementById('chat-send');
const chatProvider = document.getElementById('chat-provider');
const chatProviderMeta = document.getElementById('chat-provider-meta');

async function refreshChatProviders() {
    try {
        const r = await api('/llm-providers');
        chatProvider.innerHTML = '';
        r.providers.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.id;
            opt.textContent = p.label || p.id;
            if (p.id === r.default) opt.selected = true;
            chatProvider.appendChild(opt);
        });
        updateProviderMeta();
    } catch (e) {
        chatProviderMeta.textContent = '加载 provider 失败: ' + e.message;
    }
}
async function updateProviderMeta() {
    if (!chatProvider.value) return;
    try {
        const info = await api('/llm-info?provider_id=' + encodeURIComponent(chatProvider.value));
        chatProviderMeta.textContent = `kind=${info.kind} · model=${info.model}` + (info.base_url ? ` · ${info.base_url}` : '');
    } catch (e) {
        chatProviderMeta.textContent = '';
    }
}
chatProvider.addEventListener('change', updateProviderMeta);

function appendMsg(role, text) {
    const d = document.createElement('div');
    d.className = `chat-msg ${role}`;
    d.textContent = text;
    chatLog.appendChild(d);
    chatLog.scrollTop = chatLog.scrollHeight;
    return d;
}

// 极简 markdown:**bold** / *italic* / ## heading / 段落 / 换行
// 先 escape 防 XSS,再做替换。不支持表格/代码块等(LLM 回答不需要)
function renderMarkdown(raw) {
    let s = escapeHtml(raw);
    // 标题:## 开头
    s = s.replace(/^###\s+(.+)$/gm, '<h4>$1</h4>');
    s = s.replace(/^##\s+(.+)$/gm, '<h3>$1</h3>');
    // **bold** — 非贪婪,允许跨行内字符但不跨段落
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    // *italic* / _italic_(避免和 bold 冲突,要求两侧非星号)
    s = s.replace(/(^|[^*])\*([^*\n]+)\*([^*]|$)/g, '$1<em>$2</em>$3');
    // 段落:连续换行 → </p><p>;单换行 → <br>
    const paras = s.split(/\n{2,}/).map(p => p.trim()).filter(Boolean);
    return paras.map(p => `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');
}

// LLM 偶尔截短 photo_id 到 8 位前缀(prompt 已写铁律但不稳)— 用 candidates 集合做前缀补全
// 返回:
//   - 完整 candidate id(直接 hit 或唯一前缀匹配)→ 渲染
//   - null(候选集非空 + id 不在候选 + 没唯一前缀匹配)→ 视作 LLM 幻觉,**调用方应静默丢弃,不渲染红框**
//   - 原 id(候选集为空,无法校验)→ 渲染,onerror 兜底
function _fixPhotoId(id, candidateIds) {
    if (!candidateIds || candidateIds.size === 0) return id;
    if (candidateIds.has(id)) return id;
    const matches = [];
    for (const c of candidateIds) {
        if (c.startsWith(id)) {
            matches.push(c);
            if (matches.length > 1) break;
        }
    }
    if (matches.length === 1) return matches[0];
    return null;   // 既不在候选也无唯一前缀匹配 → 幻觉,丢
}

function _addFavStar(wrap, isFav) {
    // 收藏 ⭐ 角标,覆盖在缩略图右上角(chat / browse 复用同款)
    if (!isFav) return;
    if (!wrap.style.position) wrap.style.position = 'relative';
    const star = document.createElement('span');
    star.className = 'fav-badge';
    star.textContent = '⭐';
    star.style.cssText = 'position:absolute;top:2px;right:3px;font-size:13px;'
        + 'line-height:1;text-shadow:0 0 3px rgba(0,0,0,.6);pointer-events:none';
    wrap.appendChild(star);
}

function renderAssistantBody(div, text, candidateIds, favoriteIds) {
    // 抠 [photo:xxx] + 程序校验/修复截短 id,主体走 markdown,缩略图单独追加
    const rawIds = [...text.matchAll(/\[photo:([A-Za-z0-9_-]+)\]/g)].map(m => m[1]);
    // _fixPhotoId 返 null = 幻觉 id,filter 掉不渲染
    const ids = rawIds.map(id => _fixPhotoId(id, candidateIds)).filter(Boolean);
    const cleaned = text.replace(/\[photo:([A-Za-z0-9_-]+)\]/g, '').trim();
    // 保留可能存在的 .chat-photos 子元素(已渲染过的图就别闪烁)
    const photos = div.querySelector('.chat-photos');
    div.innerHTML = renderMarkdown(cleaned);
    if (photos) div.appendChild(photos);

    if (ids.length === 0) return;
    let pdiv = div.querySelector('.chat-photos');
    if (!pdiv) {
        pdiv = document.createElement('div');
        pdiv.className = 'chat-photos';
        div.appendChild(pdiv);
    }
    pdiv.innerHTML = '';
    [...new Set(ids)].forEach(id => {
        const wrap = document.createElement('span');
        wrap.className = 'chat-photo-wrap';
        wrap.title = id;
        const img = document.createElement('img');
        img.src = `/api/thumb/${id}`;
        img.loading = 'lazy';
        let retried = false;
        img.onerror = () => {
            // 第一次失败 → cache busting 重试一次(server 刚重启时可能 405 瞬态,浏览器 disk 缓存了失败)
            if (!retried) {
                retried = true;
                setTimeout(() => { img.src = `/api/thumb/${id}?_=${Date.now()}`; }, 300);
                return;
            }
            // 第二次还失败:LLM 幻觉的不存在 id,或缩略图 cache 还没生成
            wrap.classList.add('missing');
            wrap.title = `图片缺失 (id 不存在或未生成缩略图): ${id}`;
            wrap.textContent = '⚠ ' + id.slice(0, 8);
            img.remove();
        };
        img.onclick = () => openViewer(id);
        wrap.appendChild(img);
        _addFavStar(wrap, favoriteIds && favoriteIds.has(id));
        pdiv.appendChild(wrap);
    });
}

chatForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const q = chatInput.value.trim();
    if (!q) return;
    appendMsg('user', q);
    chatInput.value = '';
    chatSend.disabled = true;
    const metaDiv = appendMsg('meta', '思考中...');
    const respDiv = appendMsg('assistant', '');
    let buf = '';
    let toolResultItems = [];   // result 帧的 raw items(完整候选,LLM 可能只引用一部分)
    let candidateIds = new Set();   // 候选完整 id 集合(给 _fixPhotoId 做前缀补全 — LLM 偶发截短)
    let favoriteIds = new Set();    // 候选里 favorite=true 的 id 集合(给缩略图打 ⭐ 角标)
    try {
        const r = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question: q, provider_id: chatProvider.value || undefined }),
        });
        if (!r.ok) throw new Error(await r.text());
        const reader = r.body.getReader();
        const decoder = new TextDecoder();
        let pending = '';
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            pending += decoder.decode(value, { stream: true });
            // SSE 帧由 \n\n 分隔
            const frames = pending.split('\n\n');
            pending = frames.pop();
            for (const frame of frames) {
                let evt = 'message', data = '';
                for (const line of frame.split('\n')) {
                    if (line.startsWith('event:')) evt = line.slice(6).trim();
                    else if (line.startsWith('data:')) data += line.slice(5).trim();
                }
                if (!data) continue;
                let parsed; try { parsed = JSON.parse(data); } catch { parsed = data; }
                if (evt === 'phase') {
                    // 仅在还没收到 planned 时显示 phase 占位;收到 planned 后由 trace 结构接管
                    if (!metaDiv.dataset.gotPlan) {
                        metaDiv.textContent = `${{planning: '选工具', executing: '查库', answering: '生成回答'}[parsed.stage] || parsed.stage}...`;
                    }
                } else if (evt === 'planned') {
                    metaDiv.dataset.gotPlan = '1';
                    metaDiv.classList.add('chat-trace');
                    metaDiv.innerHTML = renderTrace(parsed.action, parsed.args || {}, parsed.rationale || '', '查库中...');
                } else if (evt === 'result') {
                    // 记录完整候选 items(给后面"未引用"展开用)
                    const raw = parsed.raw || {};
                    toolResultItems = raw.items || (raw.places || []).flatMap(pl =>
                        (pl.sample_photo_ids || []).map(pid => ({ photo_id: pid }))
                    );
                    // 候选 id 集合(_fixPhotoId 用 — LLM 偶发把 36 字符 uuid 截到 8 位前缀)
                    candidateIds = new Set(toolResultItems.map(it => it.photo_id).filter(Boolean));
                    favoriteIds = new Set(
                        toolResultItems.filter(it => it.favorite).map(it => it.photo_id).filter(Boolean)
                    );
                    if (metaDiv.dataset.gotPlan) {
                        // 更新 summary 字段不动其他
                        const sumEl = metaDiv.querySelector('.chat-trace-summary');
                        if (sumEl) sumEl.textContent = parsed.summary;
                    } else {
                        metaDiv.textContent = `${parsed.summary} · 生成回答中...`;
                    }
                } else if (evt === 'chunk') {
                    buf += parsed;
                    renderAssistantBody(respDiv, buf, candidateIds, favoriteIds);
                    chatLog.scrollTop = chatLog.scrollHeight;
                } else if (evt === 'done') {
                    // 在回答下方追加"未被回答引用的其他候选"折叠区
                    const citedIds = new Set(parsed.photo_ids || []);
                    const uncited = toolResultItems
                        .map(it => it.photo_id)
                        .filter(pid => pid && !citedIds.has(pid));
                    if (uncited.length > 0) {
                        appendUncitedPhotos(respDiv, uncited, favoriteIds);
                    }
                    // trace 框保留,只在末尾追加"引用 N 张"
                    if (metaDiv.dataset.gotPlan) {
                        const cite = parsed.photo_ids && parsed.photo_ids.length
                            ? ` · 回答引用 ${parsed.photo_ids.length} 张${uncited.length ? ` (+${uncited.length} 未引用)` : ''}`
                            : '';
                        const sumEl = metaDiv.querySelector('.chat-trace-summary');
                        if (sumEl && cite) sumEl.textContent = sumEl.textContent + cite;
                    } else {
                        metaDiv.textContent = parsed.photo_ids && parsed.photo_ids.length
                            ? `引用了 ${parsed.photo_ids.length} 张照片` : '完成';
                    }
                } else if (evt === 'error') {
                    metaDiv.textContent = '❌ ' + parsed;
                    metaDiv.style.color = '#c00';
                }
            }
        }
    } catch (err) {
        metaDiv.textContent = '❌ 请求失败: ' + err.message;
        metaDiv.style.color = '#c00';
    } finally {
        chatSend.disabled = false;
        chatInput.focus();
    }
});
