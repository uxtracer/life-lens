// life_lens 前端 — vanilla JS,无框架。

const api = (path, opts = {}) => fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
}).then(async r => {
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
});

// ============================================================
// Tab routing(4 顶层 tab + 扫描 tab 内 3 个子 tab)
// ============================================================

const VALID_TABS = new Set(['settings', 'faces', 'scan', 'browse', 'chat']);

function activateTab(name) {
    if (!VALID_TABS.has(name)) name = 'settings';
    document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.page === name));
    document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === name));
    // 记住当前 tab,刷新页面继续在这里
    try { localStorage.setItem('life_lens.last_tab', name); } catch (_) {}
    if (name === 'settings')     refreshSettings();
    else if (name === 'faces')   refreshClusters();
    else if (name === 'scan')    refreshScanTab();
    else if (name === 'browse')  refreshThumbs();
    else if (name === 'chat') {
        document.getElementById('chat-input').focus();
        refreshChatProviders();
    }
}

document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => activateTab(btn.dataset.page));
});

// 扫描 tab 内的 segment control(当前/历史/重跑)
function activateScanSubPage(name) {
    document.querySelectorAll('.scan-tab').forEach(b => b.classList.toggle('active', b.dataset.scanPage === name));
    document.querySelectorAll('.scan-page').forEach(p => p.classList.toggle('active', p.dataset.scanPage === name));
    if (name === 'current')        { refreshSources(); refreshScanPage(); }
    else if (name === 'runs')      refreshRuns();
    else if (name === 'reprocess') refreshReprocessForm();
}

document.querySelectorAll('.scan-tab').forEach(btn => {
    btn.addEventListener('click', () => activateScanSubPage(btn.dataset.scanPage));
});

function refreshScanTab() {
    const activeSub = document.querySelector('.scan-tab.active')?.dataset.scanPage || 'current';
    activateScanSubPage(activeSub);
}

// ============================================================
// Settings tab — 配置卡片渲染 + 顶部 banner
// ============================================================

async function refreshSettings() {
    // 1. 拉聚合就绪状态
    let status = null;
    try { status = await api('/setup/status'); }
    catch (e) { console.error('setup/status fetch failed', e); }

    renderGlobalBanner(status);
    if (status) renderConfigCards(status);

    // 2. 数据源列表(配置卡内)
    refreshSources();

    // 3. 存储信息卡(只读)
    if (status) renderStorageCard(status);
}

function renderGlobalBanner(status) {
    const el = document.getElementById('global-banner');
    if (!el) return;
    if (!status) {
        el.className = 'global-banner gb-error';
        el.classList.remove('hidden');
        el.innerHTML = `<div class="gb-msg">⚠ 无法读取配置状态(后端报错,检查服务端日志)</div>`;
        return;
    }
    const missing = [];
    if (!status.ollama.ok) missing.push('Ollama 未启动');
    else if (!status.ollama.has_vision_model) missing.push('本地视觉理解模型未就位');
    if (!status.amap.configured) missing.push('高德API');
    if (!status.llm.configured) missing.push('对话模型');
    if (status.sources.count === 0) missing.push('数据源');

    if (missing.length === 0) {
        el.classList.add('hidden');
        return;
    }
    el.className = 'global-banner';
    el.classList.remove('hidden');
    el.innerHTML = `
        <div class="gb-msg">⚠ 还需配置:${missing.join(' / ')}</div>
        <button class="gb-action">去配置 →</button>
    `;
    el.querySelector('.gb-action').onclick = () => activateTab('settings');
}

function renderConfigCards(status) {
    // 渲染每张卡的 status chip + 边框状态(ready=绿/attention=黄)+ body
    renderOllamaCard(status.ollama);
    renderAmapCard(status.amap);
    renderLLMCard(status.llm);
    renderSourcesCardStatus(status.sources);
}

function _setCardChrome(cardName, state, statusText) {
    const c = document.querySelector(`.config-card[data-card="${cardName}"]`);
    if (!c) return;
    c.classList.remove('attention', 'ready');
    if (state === 'ready') c.classList.add('ready');
    else if (state === 'attention') c.classList.add('attention');
    // 默认全展开;用户可以点 head 手动折叠
    const s = document.getElementById(`card-${cardName}-status`);
    if (s) s.textContent = statusText;
}

// 卡片头点击可折叠/展开
document.addEventListener('click', (e) => {
    const head = e.target.closest('.config-card-head');
    if (!head) return;
    if (e.target.closest('button, a, input')) return;
    head.parentElement.classList.toggle('collapsed');
});

// ---- 各卡片 body 渲染 ----

function renderOllamaCard(ollama) {
    const ready = ollama.ok && ollama.has_vision_model;
    const state = ready ? 'ready' : 'attention';
    const chipText = ollama.ok
        ? (ollama.has_vision_model ? '✅ 已就绪' : '⚠ 没拉 vision 模型')
        : '❌ 未连接';
    _setCardChrome('ollama', state, chipText);

    const body = document.getElementById('card-ollama-body');
    if (!body) return;

    const installedModels = ollama.models || [];
    const currentModel = ollama.vision_model_name || 'qwen3-vl:8b-instruct';
    const currentEndpoint = ollama.endpoint || 'http://localhost:11434';
    const modelOptions = installedModels.length
        ? installedModels.map(m => `<option value="${escapeHtml(m)}" ${m === currentModel ? 'selected' : ''}>${escapeHtml(m)}</option>`).join('')
        : `<option value="${escapeHtml(currentModel)}" selected>${escapeHtml(currentModel)}</option>`;

    // 编辑表单(配置好时折叠,未配时直接展开)
    const editForm = `
        <div class="config-form-row">
            <input type="text" id="vision-endpoint" value="${escapeHtml(currentEndpoint)}" placeholder="服务地址 例 http://localhost:11434">
            ${installedModels.length
                ? `<select id="vision-model">${modelOptions}</select>`
                : `<input type="text" id="vision-model" value="${escapeHtml(currentModel)}" placeholder="模型名 例 qwen3-vl:8b-instruct">`}
        </div>
        <div class="config-form-row">
            <button id="vision-test-btn" class="secondary">测试连接</button>
            <button id="vision-save-btn">保存</button>
            ${ready ? '<button id="vision-cancel-btn" class="secondary">取消</button>' : ''}
        </div>
        <div id="vision-result" class="config-validate-result"></div>
    `;

    if (ready) {
        // 已就绪 → 默认只展示 summary + 编辑/重新检测 按钮
        body.innerHTML = `
            <div class="config-card-ok-summary">
                ✅ 已连接 <code>${escapeHtml(currentEndpoint)}</code> · 模型 <code>${escapeHtml(currentModel)}</code>
            </div>
            <div class="config-form-row">
                <button id="vision-edit-btn" class="secondary">编辑配置</button>
                <button id="vision-recheck-btn" class="secondary">重新检测</button>
            </div>
            <div id="vision-edit-form" class="hidden">${editForm}</div>
        `;
        document.getElementById('vision-edit-btn').onclick = () => {
            const f = document.getElementById('vision-edit-form');
            f.classList.remove('hidden');
            wireVisionForm();
        };
        document.getElementById('vision-recheck-btn').onclick = refreshSettings;
    } else {
        // 未就绪 → guide + 错误条 + 表单直接展开
        const guide = `
            <div class="config-empty-guide">
                <b>视觉模型必装</b>。Ollama 是本地推理工具,life_lens 用它把图片转成文字描述。
                <br>
                ① 到 <a href="https://ollama.com/" target="_blank">https://ollama.com/</a> 按官网指南装<br>
                ② 在 terminal 跑 <code>ollama pull qwen3-vl:8b-instruct</code> 拉模型(~5GB)<br>
                ③ 跑 <code>ollama serve</code> 启动服务(默认端口 11434)<br>
                ④ 装完保存下方配置 → 检测
            </div>
        `;
        let issue = '';
        if (!ollama.ok) {
            issue = `<div class="config-issue-warn"><b>❌ Ollama 服务没连上</b>(<code>${escapeHtml(currentEndpoint)}</code>)<br>错误:${escapeHtml(ollama.error || '(unknown)')}</div>`;
        } else if (!ollama.has_vision_model) {
            const installed = installedModels.map(m => `<code>${escapeHtml(m)}</code>`).join(', ') || '(无)';
            issue = `<div class="config-issue-warn"><b>⚠ Ollama 通了,但当前配的模型 <code>${escapeHtml(currentModel)}</code> 没装</b><br>已装的模型:${installed}</div>`;
        }
        body.innerHTML = guide + issue + editForm;
        wireVisionForm();
    }
}

function wireVisionForm() {
    document.getElementById('vision-test-btn').onclick = async () => {
        const ep = document.getElementById('vision-endpoint').value.trim();
        const r = document.getElementById('vision-result');
        r.className = 'config-validate-result'; r.textContent = '探测中...';
        try {
            const res = await api('/ollama/ping?endpoint=' + encodeURIComponent(ep));
            if (res.ok) {
                r.className = 'config-validate-result ok';
                r.textContent = `✅ ${ep} 通了,${(res.models || []).length} 个本地模型`;
            } else {
                r.className = 'config-validate-result error';
                r.textContent = '❌ ' + (res.error || '失败');
            }
        } catch (e) { r.className = 'config-validate-result error'; r.textContent = '❌ ' + e.message; }
    };
    document.getElementById('vision-save-btn').onclick = async () => {
        const endpoint = document.getElementById('vision-endpoint').value.trim();
        const model = document.getElementById('vision-model').value.trim();
        const r = document.getElementById('vision-result');
        if (!endpoint || !model) { r.className = 'config-validate-result error'; r.textContent = '服务地址和模型名都不能空'; return; }
        try {
            await api('/config/vision', { method: 'POST', body: JSON.stringify({ endpoint, model }) });
            r.className = 'config-validate-result ok';
            r.textContent = '✅ 已保存,刷新检测中...';
            setTimeout(refreshSettings, 400);
        } catch (e) { r.className = 'config-validate-result error'; r.textContent = '❌ ' + e.message; }
    };
    const cancelBtn = document.getElementById('vision-cancel-btn');
    if (cancelBtn) cancelBtn.onclick = () => {
        document.getElementById('vision-edit-form').classList.add('hidden');
        document.getElementById('vision-result').textContent = '';
    };
}

function renderAmapCard(amap) {
    const state = amap.configured ? 'ready' : 'attention';
    const chipText = amap.configured
        ? `✅ 已配 · 今日 ${amap.quota?.used ?? 0}/${amap.quota?.limit ?? 4800}`
        : '❌ 未配';
    _setCardChrome('amap', state, chipText);

    const body = document.getElementById('card-amap-body');
    if (!body) return;

    const summaryStr = amap.configured
        ? `<div class="config-card-ok-summary">✅ 已配高德 API key · 今日已用 ${amap.quota?.used ?? 0}/${amap.quota?.limit ?? 4800} 次配额${amap.quota?.exhausted ? ' <b style="color:#c00">(已耗尽,次日 0:00 重置)</b>' : ''}</div>`
        : `<div class="config-empty-guide">
            系统需要用高德API把照片里的 GPS 坐标转成城市和地点名称,"我去过哪些地方"、“北京的照片”这类查询要靠它。
            <br><br>
            ① 到 <a href="https://lbs.amap.com/" target="_blank">https://lbs.amap.com/</a> 注册账号<br>
            ② 控制台 → 应用管理 → 添加应用 → 添加 key,服务平台选 <b>Web 服务</b><br>
            ③ 把 key 粘贴下面输入框 → 点验证<br>
            ④ 验证通过后点保存
        </div>`;

    body.innerHTML = `
        ${summaryStr}
        <div class="config-form-row">
            <input type="password" id="amap-key-input" placeholder="${amap.configured ? '修改新 key(留空保留当前)' : 'sk- 或 32 位字符'}" autocomplete="off">
            <button id="amap-validate-btn" class="secondary">验证 key</button>
            <button id="amap-save-btn">保存</button>
        </div>
        <div id="amap-validate-result" class="config-validate-result"></div>
    `;

    document.getElementById('amap-validate-btn').onclick = async () => {
        const key = document.getElementById('amap-key-input').value.trim();
        const r = document.getElementById('amap-validate-result');
        if (!key) { r.className = 'config-validate-result error'; r.textContent = '请先填 key'; return; }
        r.className = 'config-validate-result'; r.textContent = '验证中...';
        try {
            const res = await api('/config/amap-key/validate', { method: 'POST', body: JSON.stringify({ key }) });
            if (res.ok) {
                r.className = 'config-validate-result ok';
                r.textContent = '✅ 验证通过'
            } else {
                r.className = 'config-validate-result error';
                r.textContent = '❌ ' + (res.error || '失败');
            }
        } catch (e) { r.className = 'config-validate-result error'; r.textContent = '❌ ' + e.message; }
    };

    document.getElementById('amap-save-btn').onclick = async () => {
        const key = document.getElementById('amap-key-input').value.trim();
        const r = document.getElementById('amap-validate-result');
        if (!key) { r.className = 'config-validate-result error'; r.textContent = '请先填 key'; return; }
        try {
            await api('/config/amap-key', { method: 'POST', body: JSON.stringify({ key }) });
            r.className = 'config-validate-result ok';
            r.textContent = '✅ 已保存,正在刷新...';
            setTimeout(refreshSettings, 500);
        } catch (e) { r.className = 'config-validate-result error'; r.textContent = '❌ ' + e.message; }
    };
}

function renderLLMCard(llm) {
    const state = llm.configured ? 'ready' : 'attention';
    const chipText = llm.configured
        ? `✅ ${llm.count} 个 provider · 默认 ${llm.default}`
        : '❌ 未配';
    _setCardChrome('llm', state, chipText);

    const body = document.getElementById('card-llm-body');
    if (!body) return;

    body.innerHTML = `
        ${llm.configured ? '' : `<div class="config-empty-guide">
            系统用大语言模型生成回答 — 任何兼容 OpenAI <code>/v1/chat/completions</code> 格式的服务都可接(填 api_key + base_url + model)，云端、本地都可以。
            <br><br>
            <b>不知道选啥?</b> 系统的对话调用并不复杂(选工具 + 写中文回答),像<b>DeepSeek v4-flash</b> 这种高性价比模型即可。<br>
            申请:① 到 <a href="https://platform.deepseek.com/" target="_blank">https://platform.deepseek.com/</a> 注册 ② 创建 API key
            ③ 把 key + <code>https://api.deepseek.com/v1</code> + 模型 <code>deepseek-v4-flash</code> 填下面表单
            <br><br>
            其他可选:OpenAI gpt-4o-mini / Together / 任何本地 LLM 服务。
        </div>`}
        <ul id="llm-provider-list" class="list"></ul>
        <details style="margin-top:12px">
            <summary style="cursor:pointer;color:#2563eb;font-size:13px">➕ 添加 LLM provider</summary>
            <div style="background:#f9fafb;padding:12px;border-radius:6px;margin-top:8px">
                <div class="config-form-row">
                    <label style="flex:1">Provider ID(随便起,英文)
                        <input type="text" id="llm-new-id" placeholder="例如 deepseek / openai" style="margin-top:4px">
                    </label>
                </div>
                <div class="config-form-row">
                    <label style="flex:1">Model
                        <input type="text" id="llm-new-model" placeholder="deepseek-v4-flash / gpt-4o-mini" style="margin-top:4px">
                    </label>
                </div>
                <div class="config-form-row">
                    <label style="flex:1">Base URL
                        <input type="text" id="llm-new-baseurl" placeholder="https://api.deepseek.com/v1" style="margin-top:4px">
                    </label>
                </div>
                <div class="config-form-row">
                    <label style="flex:1">API Key
                        <input type="password" id="llm-new-key" placeholder="sk-..." style="margin-top:4px">
                    </label>
                </div>
                <div class="config-form-row">
                    <label style="flex:1">Label(显示用)
                        <input type="text" id="llm-new-label" placeholder="DeepSeek (便宜+中文好)" style="margin-top:4px">
                    </label>
                    <button id="llm-add-btn">添加</button>
                </div>
                <div id="llm-add-result" class="config-validate-result"></div>
            </div>
        </details>
    `;

    // 渲染已有 providers
    refreshLLMProviderList(llm);

    document.getElementById('llm-add-btn').onclick = async () => {
        const provider_id = document.getElementById('llm-new-id').value.trim();
        const model       = document.getElementById('llm-new-model').value.trim();
        const base_url    = document.getElementById('llm-new-baseurl').value.trim();
        const api_key     = document.getElementById('llm-new-key').value.trim();
        const label       = document.getElementById('llm-new-label').value.trim();
        const r = document.getElementById('llm-add-result');
        // 编辑场景:provider 已存在 → api_key 可留空(后端保留旧 key);其他字段仍必填
        const isEditing = _llmProvidersCache.some(p => p.id === provider_id);
        if (!provider_id || !model || !base_url || (!isEditing && !api_key)) {
            r.className = 'config-validate-result error';
            r.textContent = isEditing ? 'ID / model / base_url 都不能空' : 'ID / model / base_url / api_key 都不能空';
            return;
        }
        // 编辑模式 api_key 空 → 不传字段,后端会保留旧 key
        const cfg = { kind: 'openai-compat', model, base_url, label: label || provider_id };
        if (api_key) cfg.api_key = api_key;
        try {
            await api('/config/llm-provider', {
                method: 'POST',
                body: JSON.stringify({ op: 'upsert', provider_id, config: cfg }),
            });
            r.className = 'config-validate-result ok';
            r.textContent = isEditing ? '✅ 已保存,刷新中...' : '✅ 添加成功,刷新中...';
            setTimeout(refreshSettings, 400);
        } catch (e) {
            r.className = 'config-validate-result error';
            r.textContent = '❌ ' + e.message;
        }
    };
}

let _llmProvidersCache = [];   // 缓存最近一次拉到的 provider list 给编辑用

async function refreshLLMProviderList(llm) {
    const list = document.getElementById('llm-provider-list');
    if (!list) return;
    if (llm.count === 0) { list.innerHTML = '<li class="hint" style="border:0;background:transparent;padding:0">(还没有 provider — 用下方表单添加)</li>'; return; }

    // /api/llm-providers 给详细列表(public,无 api_key)
    const { providers, default: defaultId } = await api('/llm-providers');
    _llmProvidersCache = providers;
    list.innerHTML = providers.map(p => `
        <li${p.id === defaultId ? ' class="is-default"' : ''}>
            <span><b>${escapeHtml(p.label)}</b> — ${escapeHtml(p.model || '')}${p.id === defaultId ? ' <span class="llm-default-badge">默认</span>' : ''}</span>
            <span class="row-actions">
                ${p.id === defaultId ? '' : `<button class="action" data-act="default" data-pid="${p.id}">设为默认</button>`}
                <button class="action" data-act="edit" data-pid="${p.id}">编辑</button>
                <button class="del" data-act="del" data-pid="${p.id}">删除</button>
            </span>
        </li>
    `).join('');
    list.querySelectorAll('button[data-act]').forEach(btn => {
        btn.onclick = async () => {
            const pid = btn.dataset.pid;
            const act = btn.dataset.act;
            if (act === 'edit') { _openLLMEdit(pid); return; }
            try {
                if (act === 'default') {
                    await api('/config/llm-default', { method: 'POST', body: JSON.stringify({ provider_id: pid }) });
                } else if (act === 'del') {
                    if (!confirm(`删除 provider ${pid}?`)) return;
                    await api('/config/llm-provider', { method: 'POST', body: JSON.stringify({ op: 'delete', provider_id: pid }) });
                }
                refreshSettings();
            } catch (e) { alert('失败: ' + e.message); }
        };
    });
}

function _openLLMEdit(pid) {
    const p = _llmProvidersCache.find(x => x.id === pid);
    if (!p) return;
    // 打开 <details> 折叠区,预填字段,api_key 留空提示"保留原 key"
    const details = document.querySelector('#card-llm-body details');
    if (details) details.open = true;
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ''; };
    set('llm-new-id',      p.id);
    set('llm-new-model',   p.model);
    set('llm-new-baseurl', p.base_url);
    set('llm-new-label',   p.label);
    const keyInput = document.getElementById('llm-new-key');
    if (keyInput) {
        keyInput.value = '';
        keyInput.placeholder = '留空 = 保留原 key(只想改其他字段时用)';
    }
    // ID 不让改(改 ID 会变成新建)
    const idInput = document.getElementById('llm-new-id');
    if (idInput) { idInput.readOnly = true; idInput.style.background = '#f3f4f6'; }
    // 按钮文案改 "保存修改"
    const btn = document.getElementById('llm-add-btn');
    if (btn) btn.textContent = '保存修改';
    // 加一个"取消编辑"按钮(只加一次)
    if (btn && !document.getElementById('llm-cancel-edit')) {
        const c = document.createElement('button');
        c.id = 'llm-cancel-edit';
        c.type = 'button';
        c.className = 'secondary';
        c.textContent = '取消';
        c.onclick = () => {
            idInput.readOnly = false; idInput.style.background = '';
            ['llm-new-id','llm-new-model','llm-new-baseurl','llm-new-label','llm-new-key'].forEach(i => {
                const el = document.getElementById(i); if (el) el.value = '';
            });
            keyInput.placeholder = 'sk-...';
            btn.textContent = '添加';
            const r = document.getElementById('llm-add-result');
            if (r) { r.textContent = ''; r.className = 'config-validate-result'; }
            // 关掉折叠区(回到 "已配 LLM" 简洁状态)
            if (details) details.open = false;
            c.remove();
        };
        btn.parentNode.appendChild(c);
    }
    // 滚到表单
    details.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderSourcesCardStatus(sources) {
    const state = sources.count > 0 ? 'ready' : 'attention';
    const chipText = sources.count > 0 ? `✅ ${sources.count} 个源` : '❌ 未添加';
    _setCardChrome('sources', state, chipText);
    // body 由 refreshSources() 渲染 #source-list,这里不重复
}

function renderStorageCard(status) {
    const body = document.getElementById('card-storage-body');
    if (!body) return;
    body.innerHTML = `
        <p class="hint">主库 / 缓存 / 备份都在 <code>~/.life_lens/</code>。</p>
        <ul style="font-size:13px;line-height:2;color:#374151;padding-left:18px;margin:0">
            <li>lens.db — SQLite 主库(${status.photos.count.toLocaleString()} 张照片已扫)</li>
            <li>.cache/preprocessed/ — 1024px JPEG 缩略图缓存</li>
            <li>backups/ — schema 升级时自动备份</li>
            <li>chat_log/ — 每次对话的 jsonl 日志</li>
        </ul>
        <div class="embeddings-box" id="embeddings-box">加载中……</div>
    `;
    refreshEmbeddingsBox();
}

// 语义索引覆盖率 + 重建按钮。常规扫描会 inline 写,这里只是兜底
// (fastembed 没装过 / 换模型 / 改 source_text 拼装规则)。
let _embRebuildTimer = null;
async function refreshEmbeddingsBox() {
    const box = document.getElementById('embeddings-box');
    if (!box) return;
    let data;
    try {
        data = await api('/embeddings/status');
    } catch (e) {
        box.innerHTML = `<p class="hint" style="color:#b91c1c">语义索引状态加载失败:${escapeHtml(e.message || e)}</p>`;
        return;
    }
    const total = data.total_with_vision;
    const idx = data.total_indexed;
    const missing = data.missing;
    const pct = total ? Math.round(idx * 100 / total) : 0;
    const reb = data.rebuild || {};
    const running = !!reb.running;

    let progressHTML = '';
    if (running) {
        const rTotal = reb.total || 0;
        const rDone = reb.done || 0;
        const rPct = rTotal ? Math.round(rDone * 100 / rTotal) : 0;
        progressHTML = `
            <div class="hint" style="margin-top:6px">
                正在重建:${rDone.toLocaleString()} / ${rTotal.toLocaleString()}(${rPct}%)${reb.failed ? ` · 失败 ${reb.failed}` : ''}
            </div>
            <div class="progress-bar"><div class="progress-bar-fill" style="width:${rPct}%"></div></div>
        `;
    } else if (reb.finished_at && !reb.error) {
        progressHTML = `<div class="hint" style="margin-top:4px;color:#16a34a">✓ 上次重建完成于 ${reb.finished_at}</div>`;
    } else if (reb.error) {
        progressHTML = `<div class="hint" style="margin-top:4px;color:#b91c1c">✗ ${escapeHtml(reb.error)}</div>`;
    }

    box.innerHTML = `
        <h4 style="margin:14px 0 6px;font-size:14px">语义索引（用于“问相册”的近义检索）</h4>
        <p class="hint" style="margin:0 0 6px">
            主扫描会自动建索引,这里是兜底。覆盖率 <b>${idx.toLocaleString()} / ${total.toLocaleString()}</b>(${pct}%)${missing ? ` · 待建 ${missing.toLocaleString()}` : ''}${data.model ? ` · 模型 ${escapeHtml(data.model)}` : ''}
        </p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
            <button type="button" id="emb-rebuild-incr" ${running || missing === 0 ? 'disabled' : ''}>
                ${missing === 0 ? '已全部建索引' : `补建 ${missing.toLocaleString()} 张缺失`}
            </button>
            <button type="button" id="emb-rebuild-force" class="secondary" ${running || total === 0 ? 'disabled' : ''}>全量重建(换模型 / 改拼装规则用)</button>
        </div>
        ${progressHTML}
    `;

    const incr = document.getElementById('emb-rebuild-incr');
    if (incr) incr.onclick = () => startEmbRebuild(false);
    const force = document.getElementById('emb-rebuild-force');
    if (force) force.onclick = () => {
        if (!confirm(`全量重建会重新嵌入所有 ${total.toLocaleString()} 张,可能要 ${Math.ceil(total / 87 / 60)} 分钟左右。继续吗?`)) return;
        startEmbRebuild(true);
    };

    // 跑着的时候每 2s 拉一次
    if (running) {
        if (!_embRebuildTimer) {
            _embRebuildTimer = setInterval(refreshEmbeddingsBox, 2000);
        }
    } else if (_embRebuildTimer) {
        clearInterval(_embRebuildTimer);
        _embRebuildTimer = null;
    }
}

async function startEmbRebuild(force) {
    try {
        await api('/embeddings/rebuild', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ force }),
        });
    } catch (e) {
        alert('启动失败:' + (e.message || e));
        return;
    }
    refreshEmbeddingsBox();
}

function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}


// 启动:优先恢复上次的 tab(localStorage);没有的话才看 setup status next_step
(async function init() {
    let target = null;
    try { target = localStorage.getItem('life_lens.last_tab'); } catch (_) {}
    if (target && VALID_TABS.has(target)) {
        activateTab(target);
        return;
    }
    // 首次访问 / localStorage 没值 → 看 setup status 决定
    try {
        const status = await api('/setup/status');
        const nextStep = status.next_step;
        target = nextStep === 'browse' ? 'browse' :
                 nextStep === 'scan'   ? 'scan'   : 'settings';
    } catch (e) {
        console.error('初始 setup/status 拉取失败', e);
        target = 'settings';
    }
    activateTab(target);
})();

// ---- Sources ----
let _allSources = [];
async function refreshSources() {
    const { sources } = await api('/sources');
    _allSources = sources;
    const list = document.getElementById('source-list');
    list.innerHTML = '';
    sources.forEach(s => {
        const li = document.createElement('li');
        li.innerHTML = `<span><b>${s.kind}</b> — ${s.config.path || s.source_id}${s.last_scan_at ? ` · 上次扫描 ${s.last_scan_at}` : ''}</span>`;
        const del = document.createElement('button');
        del.className = 'del';
        del.textContent = '删除';
        del.onclick = async () => {
            if (!confirm('删除这个数据源?(不会删除已扫数据)')) return;
            await api('/sources/' + encodeURIComponent(s.source_id), { method: 'DELETE' });
            refreshSources();
        };
        li.appendChild(del);
        list.appendChild(li);
    });
    // 同步渲染扫描页的 source 列表
    renderScanSourceList(sources);
}

function renderScanSourceList(sources) {
    const el = document.getElementById('scan-source-list');
    if (!el) return;
    el.innerHTML = '';
    if (sources.length === 0) {
        el.innerHTML = '<p class="hint">还没添加数据源。先去「数据源」页添加。</p>';
        return;
    }
    sources.forEach(s => {
        const li = document.createElement('div');
        li.className = 'scan-source-row';
        li.innerHTML = `
            <div>
                <div><b>${s.kind}</b> · <code>${s.source_id}</code></div>
                <div class="hint" style="margin:2px 0 0">${s.config.path || ''}</div>
            </div>
            <button class="scan-single">扫描这个</button>
        `;
        li.querySelector('.scan-single').onclick = () => startScan([s.source_id]);
        el.appendChild(li);
    });
}

document.getElementById('add-source-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const path = document.getElementById('source-path').value.trim();
    // .photoslibrary 结尾 → Apple Photos kind;否则 filesystem
    const kind = path.endsWith('.photoslibrary') ? 'photos_library' : 'filesystem';
    try {
        await api('/sources', {
            method: 'POST',
            body: JSON.stringify({ kind, path }),
        });
        document.getElementById('source-path').value = '';
        refreshSources();
        refreshSettings();   // 刷新顶部 banner + 状态
    } catch (err) {
        alert('添加失败:' + err.message);
    }
});

// 文件夹选择器(macOS:走后端 osascript 弹原生对话框)
document.getElementById('pick-folder-btn').addEventListener('click', async () => {
    const btn = document.getElementById('pick-folder-btn');
    btn.disabled = true;   // 不改文字,但 disabled 给 CSS 视觉反馈
    try {
        const r = await api('/sources/pick-folder', { method: 'POST' });
        if (r.cancelled) return;
        if (r.path) document.getElementById('source-path').value = r.path;
    } catch (err) {
        alert('打开文件夹选择器失败:' + err.message + '\n你可以手动粘贴路径。');
    } finally {
        btn.disabled = false;
        btn.blur();
    }
});

// ---- Scan (v2:db-driven + 时间维度) ----
let _scanPollTimer = null;

async function refreshScanPage() {
    const s = await api('/status');
    renderScanGlobal(s);
    renderProgressCard(s.current_run);
    // 启动轮询
    if (!_scanPollTimer) {
        _scanPollTimer = setInterval(async () => {
            // 只在扫描页活跃时轮询
            if (!document.getElementById('scan').classList.contains('active')) return;
            const s2 = await api('/status');
            renderScanGlobal(s2);
            renderProgressCard(s2.current_run);
        }, 2000);
    }
}

function renderScanGlobal(s) {
    const el = document.getElementById('scan-global');
    if (!el) return;
    const cur = s.current_run;
    const resumableRuns = s.resumable_runs || (s.resumable_run ? [s.resumable_run] : []);
    const g = s.global || {};

    let html = `<div class="scan-summary">
        <span><b>累计统计</b></span>
        <span>已完成 <b>${g.jobs_done || 0}</b></span>
        <span>待处理 <b>${g.jobs_pending || 0}</b></span>
        <span>失败 <b>${g.jobs_failed || 0}</b></span>
    </div>`;

    // 高德今日配额
    const q = s.amap_quota;
    if (q) {
        const pct = q.limit ? Math.round(q.used / q.limit * 100) : 0;
        let cls = 'amap-quota';
        if (q.exhausted) cls += ' exhausted';
        else if (pct >= 80) cls += ' near-limit';
        html += `<div class="${cls}">
            高德今日 <b>${q.used} / ${q.limit}</b> (${pct}%)
            ${q.exhausted ? ` · <b>已耗尽</b>,${q.next_reset_at} 重置后可继续` : ` · ${q.date_local} (UTC+8)`}
        </div>`;
    }

    if (cur) {
        html += `<div class="scan-cta running">
            <b>正在扫描 ${cur.run_id}</b>
            <button id="scan-stop-btn" class="danger">${cur.stop_requested ? '收尾中…' : '暂停扫描'}</button>
            <span class="hint" style="margin:0 0 0 8px">暂停 = 让当前照片跑完后退出，下次点"继续"继续执行任务</span>
        </div>`;
    } else {
        if (resumableRuns.length > 0) {
            const plural = resumableRuns.length > 1 ? ` (${resumableRuns.length} 个)` : '';
            html += `<div class="scan-resumable-header"><b>未完成的扫描${plural}</b> — 点对应卡片的"继续扫描"恢复(同时间只能跑一个)</div>`;
            resumableRuns.forEach((res, i) => {
                const srcs = (res.source_ids || []).join(', ') || '(无)';
                const tr = res.time_range || {};
                const pct = res.total ? Math.round((res.done + res.failed) / res.total * 100) : 0;
                html += `<div class="scan-cta resumable">
                    <div style="flex:1">
                        <div><b>${res.status === 'failed' ? '失败' : '已暂停'}</b> · <code>${res.run_id}</code> <span class="pc-kind">${res.kind || ''}</span></div>
                        <div class="hint" style="margin:4px 0 0">
                            Source: ${srcs}<br>
                            进度: <b>${res.done + res.failed}/${res.total}</b> (${pct}%) · done ${res.done} · failed ${res.failed} · pending <b>${res.pending}</b><br>
                            时间范围: ${tr.min || '—'} ~ ${tr.max || '—'}${res.scanned_up_to ? ` · 已扫到 <b>${res.scanned_up_to}</b>` : ''}<br>
                            开始: ${res.started_at || '?'} · ${res.status === 'failed' ? '失败' : '暂停'}: ${res.stopped_at || '?'}
                            ${res.note ? `<br>备注: ${res.note}` : ''}
                        </div>
                    </div>
                    <button data-resume-runid="${res.run_id}" class="primary">继续扫描</button>
                </div>`;
            });
        } else {
            html += `<div class="scan-cta">
                <button id="scan-start-btn" class="primary">开始新扫描(所有 enabled source)</button>
            </div>`;
        }
    }
    el.innerHTML = html;

    const stop = document.getElementById('scan-stop-btn');
    if (stop) stop.onclick = stopScan;
    el.querySelectorAll('[data-resume-runid]').forEach(btn => {
        btn.onclick = () => resumeScan(btn.dataset.resumeRunid);
    });
    const start = document.getElementById('scan-start-btn');
    if (start) start.onclick = () => startScan();
}

function renderProgressCard(run, mountId = 'progress-card-mount') {
    const el = document.getElementById(mountId);
    if (!el) return;
    if (!run) { el.innerHTML = ''; return; }

    // Phase A:Phase B 还没开始,数量未知。显示一行文字提示就好,不画进度条
    if (run.phase === 'enqueueing') {
        el.innerHTML = `
        <div class="progress-card">
            <div class="pc-head">
                <span class="pc-runid"><b>${run.run_id}</b> <span class="pc-kind">${run.kind || ''}</span></span>
                <span class="pc-status pc-running">running</span>
            </div>
            <div class="pc-enqueueing">
                <span class="pc-spin"></span>
                正在遍历图片库提取照片元信息……
            </div>
            <div class="hint" style="margin-top:6px">
                几万张库需要 5-10 分钟，完成后会切到处理进度条。
            </div>
        </div>`;
        return;
    }

    const tr = run.time_range || {};
    const total = run.total || 0;
    const done = run.done || 0;
    const failed = run.failed || 0;
    const processing = run.processing || 0;
    const pending = run.pending || 0;
    const pct = total ? ((done + failed) / total * 100) : 0;
    const tlo = tr.min || '';
    const thi = tr.max || '';
    const scanned = run.scanned_up_to || '';
    const rate = run.rate;
    const eta = run.eta_seconds;
    const elapsed = run.elapsed_seconds;
    const isFinished = run.status === 'completed' || run.status === 'stopped' || run.status === 'failed';
    const rateLabel = isFinished ? '平均速率' : 'rate';
    const etaStr = eta ? formatDuration(eta) : '—';
    const elapsedStr = elapsed != null ? formatDuration(elapsed) : '—';

    el.innerHTML = `
    <div class="progress-card">
        <div class="pc-head">
            <span class="pc-runid"><b>${run.run_id}</b> <span class="pc-kind">${run.kind || ''}</span></span>
            <span class="pc-status pc-${run.status}">${run.status || ''}</span>
        </div>

        <div class="pc-label">数量进度</div>
        <div class="pc-bar">
            <div class="pc-bar-fill" style="width:${pct}%"></div>
        </div>
        <div class="pc-bar-meta">
            ${done + failed} / ${total} · ${pct.toFixed(1)}%
            · done <b>${done}</b> · failed <b>${failed}</b> · processing ${processing} · pending ${pending}
        </div>

        <div class="pc-label">时间进度</div>
        <div class="pc-bar-meta">
            扫描区间:<b>${tlo || '—'}</b> ~ <b>${thi || '—'}</b><br>
            ${scanned ? `已扫到拍照时间:<b>${scanned}</b>` : '尚未开始'}
        </div>

        <div class="pc-stats">
            <span>${rateLabel}: <b>${formatRate(rate)}</b></span>
            <span>用时: <b>${elapsedStr}</b></span>
            ${isFinished ? '' : `<span>ETA: <b>${etaStr}</b></span>`}
        </div>

        ${run.current_path ? `
        <div class="pc-current">
            ${run.current_thumb_url ? `<img src="${run.current_thumb_url}" loading="lazy" onerror="this.style.display='none'">` : ''}
            <div>
                <div class="pc-current-path">${run.current_path.split('/').pop()}</div>
                <div class="hint" style="margin:2px 0">${run.current_captured_at || ''}</div>
                <div class="hint" style="margin:0;font-size:11px">${run.current_path}</div>
            </div>
        </div>` : ''}
    </div>`;
}

function formatRate(rate) {
    if (!rate || rate <= 0) return '—';
    if (rate >= 1) return `${rate.toFixed(2)} 张/秒`;
    // 慢于 1 张/秒 → 反过来显示秒/张,人脑友好
    const secsPerPhoto = 1 / rate;
    if (secsPerPhoto < 60) return `${secsPerPhoto.toFixed(1)} 秒/张`;
    const m = Math.floor(secsPerPhoto / 60);
    const s = Math.round(secsPerPhoto % 60);
    return `${m}m${s}s/张`;
}

function formatDuration(secs) {
    secs = Math.round(secs);
    if (secs < 60) return secs + 's';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    return `${h}h ${m}m`;
}

async function startScan(sourceIds) {
    try {
        const body = sourceIds ? { source_ids: sourceIds } : {};
        await api('/scan', { method: 'POST', body: JSON.stringify(body) });
        alert('扫描已启动，进度会在下方实时显示。');
        refreshScanPage();
    } catch (e) {
        alert('启动失败:' + e.message);
    }
}

async function resumeScan(runId) {
    try {
        const path = runId ? `/runs/${encodeURIComponent(runId)}/resume` : '/scan/resume';
        await api(path, { method: 'POST' });
        alert('已继续扫描，进度会在下方实时显示。');
        refreshScanPage();
    } catch (e) {
        alert('继续失败:' + e.message);
    }
}

async function stopScan() {
    if (!confirm('暂停扫描？当前正在处理的照片会跑完再退出。下次点「继续」从这次扫描续上，已完成的不会重跑。')) return;
    try {
        await api('/scan/stop', { method: 'POST' });
        refreshScanPage();
    } catch (e) {
        alert('暂停失败:' + e.message);
    }
}

// ---- Runs ----

async function refreshRuns() {
    const { runs } = await api('/runs');
    const el = document.getElementById('runs-list');
    if (!el) return;
    if (!runs || runs.length === 0) {
        el.innerHTML = '<p class="hint">还没有 Run。去「扫描」页开始第一次。</p>';
        return;
    }
    el.innerHTML = `
        <table class="runs-table">
            <thead><tr>
                <th>Run ID</th><th>kind</th><th>状态</th>
                <th>total</th><th>done</th><th>failed</th>
                <th>开始</th><th>结束</th><th>备注</th>
            </tr></thead>
            <tbody>
            ${runs.map(r => `
                <tr class="run-row" data-runid="${r.run_id}">
                    <td><code>${r.run_id}</code></td>
                    <td>${r.kind}</td>
                    <td><span class="pc-status pc-${r.status}">${r.status}</span></td>
                    <td>${r.total}</td>
                    <td>${r.done}</td>
                    <td>${r.failed}</td>
                    <td class="ts">${r.started_at || ''}</td>
                    <td class="ts">${r.finished_at || ''}</td>
                    <td>${r.note || ''}</td>
                </tr>
                <tr class="run-detail-row" data-runid="${r.run_id}"><td colspan="9"><div class="run-detail-mount"></div></td></tr>
            `).join('')}
            </tbody>
        </table>`;
    el.querySelectorAll('.run-row').forEach(row => {
        row.onclick = () => toggleRunDetail(row.dataset.runid);
    });
}

async function toggleRunDetail(runId) {
    const detailRow = document.querySelector(`.run-detail-row[data-runid="${runId}"]`);
    if (!detailRow) return;
    const mount = detailRow.querySelector('.run-detail-mount');
    if (detailRow.classList.contains('open')) {
        detailRow.classList.remove('open');
        mount.innerHTML = '';
        return;
    }
    detailRow.classList.add('open');
    mount.innerHTML = '<p class="hint">加载中...</p>';
    try {
        const [run, failuresResp] = await Promise.all([
            api('/runs/' + encodeURIComponent(runId)),
            api('/runs/' + encodeURIComponent(runId) + '/failures'),
        ]);
        // 拼装一个 run 对象给 progress card 用
        const cardRun = {
            ...run,
            ...run.stats,
            run_id: runId,
            time_range: run.time_range,
            scanned_up_to: run.scanned_up_to,
            status: run.status,
            kind: run.kind,
        };
        const failures = failuresResp.failures || [];
        const pcId = 'rd-pc-' + runId;
        mount.innerHTML = `
            <div class="run-detail">
                <div id="${pcId}" class="run-detail-progress"></div>
                <div class="run-detail-actions">
                    ${(run.status === 'stopped' || run.status === 'failed') && run.stats.pending > 0
                        ? `<button class="primary" data-act="resume">继续这个 run(${run.stats.pending} 张待处理)</button>` : ''}
                    ${run.stats.failed > 0 ? `<button data-act="retry">重试失败的 ${run.stats.failed} 张</button>` : ''}
                </div>
                ${failures.length > 0 ? `
                    <div class="run-failures">
                        <h4>失败照片 (${failures.length})</h4>
                        ${failures.map(f => `
                            <div class="run-failure">
                                <img src="/api/thumb/${f.photo_id}" loading="lazy" onerror="this.style.display='none'">
                                <div>
                                    <div><b>${(f.original_path||'').split('/').pop()}</b> · ${f.captured_at_local || ''}</div>
                                    <div class="hint" style="margin:2px 0">${f.original_path || ''}</div>
                                    <div class="hint err">retry=${f.retry_count} · ${escapeHtml(f.last_error || '')}</div>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                ` : ''}
            </div>`;
        renderProgressCard(cardRun, pcId);
        const resumeBtn = mount.querySelector('[data-act="resume"]');
        if (resumeBtn) resumeBtn.onclick = () => resumeScan(runId);
        const retryBtn = mount.querySelector('[data-act="retry"]');
        if (retryBtn) retryBtn.onclick = async () => {
            if (!confirm(`重试该 run 的 ${run.stats.failed} 张失败照片?`)) return;
            try {
                const r = await api(`/runs/${encodeURIComponent(runId)}/retry`, { method: 'POST' });
                alert(`已重置 ${r.reset_count} 张失败照片,扫描已启动。`);
                refreshRuns();
            } catch (e) {
                alert('重试失败:' + e.message);
            }
        };
    } catch (e) {
        mount.innerHTML = `<p class="err">加载失败:${e.message}</p>`;
    }
}

// ---- Reprocess ----

async function refreshReprocessForm() {
    const sel = document.getElementById('rf-source');
    if (!sel) return;
    sel.innerHTML = '';
    _allSources.forEach(s => {
        const o = document.createElement('option');
        o.value = s.source_id;
        o.textContent = `${s.kind} — ${s.config.path || s.source_id}`;
        sel.appendChild(o);
    });
    document.getElementById('rf-preview').innerHTML = '';
    const runBtn = document.getElementById('rf-run-btn');
    if (runBtn) delete runBtn.dataset.count;

    // 人物筛选下拉(无人/单人/多人 + 所有已命名人物 "含 XXX")
    const personSel = document.getElementById('rf-person');
    if (personSel) {
        personSel.innerHTML = `
            <option value="">(不筛)</option>
            <option value="count:none">无人(face_count = 0)</option>
            <option value="count:single">单人(face_count = 1)</option>
            <option value="count:multi">多人(face_count ≥ 2)</option>
        `;
        try {
            const { persons } = await api('/persons');
            persons.forEach(p => {
                const o = document.createElement('option');
                o.value = `has:${p.cluster_id}`;
                o.textContent = `含 ${p.name} (${p.total_face_count || 0} 张)`;
                personSel.appendChild(o);
            });
        } catch (e) { /* ignore */ }
    }
}

function _readReprocessSelector() {
    const sourceIds = [...document.getElementById('rf-source').selectedOptions].map(o => o.value);
    // 人物筛选:单选 value 形如 'count:single' / 'has:seed_xxx'
    const personVal = document.getElementById('rf-person').value || '';
    let personCount = null;
    let personIds = null;
    if (personVal.startsWith('count:')) {
        personCount = personVal.slice('count:'.length);
    } else if (personVal.startsWith('has:')) {
        personIds = [personVal.slice('has:'.length)];
    }
    return {
        source_ids: sourceIds.length ? sourceIds : null,
        time_from: document.getElementById('rf-time-from').value || null,
        time_to:   document.getElementById('rf-time-to').value || null,
        missing_field: document.getElementById('rf-missing').value || null,
        person_count: personCount,
        person_ids: personIds,
        fts_query: document.getElementById('rf-fts').value.trim() || null,
    };
}

async function rfDoPreview() {
    const sel = _readReprocessSelector();
    const stage = document.getElementById('rf-stage').value;
    const preview = document.getElementById('rf-preview');
    preview.innerHTML = '<p class="hint">查询中...</p>';
    try {
        const r = await api('/reprocess/preview', {
            method: 'POST',
            body: JSON.stringify({ selector: sel, stage }),
        });
        if (r.count === 0) {
            preview.innerHTML = '<p class="hint">没有匹配的照片。</p>';
            delete document.getElementById('rf-run-btn').dataset.count;
            return 0;
        }
        preview.innerHTML = `
            <p><b>匹配 ${r.count} 张</b> · 阶段 ${stage}${stage === 'vision' ? `(预计 ~${Math.round(r.count * 22 / 60)} 分钟)` : ''}</p>
            <div class="rf-samples">
                ${r.sample.map(s => `<div><img src="${s.thumb_url}" loading="lazy"><div class="hint" style="margin:0">${s.captured_at || ''}</div></div>`).join('')}
            </div>
        `;
        document.getElementById('rf-run-btn').dataset.count = r.count;
        return r.count;
    } catch (err) {
        preview.innerHTML = `<p class="err">${err.message}</p>`;
        return null;
    }
}

document.addEventListener('click', async (e) => {
    if (e.target && e.target.id === 'rf-preview-btn') {
        await rfDoPreview();
    }
    if (e.target && e.target.id === 'rf-run-btn') {
        // 没预览过 → 先预览,让用户看到匹配多少再 confirm
        let count = e.target.dataset.count;
        if (count === undefined) {
            count = await rfDoPreview();
            if (!count) return;   // 0 / 失败 都不跑
        }
        const sel = _readReprocessSelector();
        const stage = document.getElementById('rf-stage').value;
        const note = document.getElementById('rf-note').value.trim() || null;
        const stageNames = { vision: '描述识别(vision)', derived: '派生字段(derived)', faces: '人脸匹配(faces)' };
        if (!confirm(`确定要对 ${count} 张照片重跑「${stageNames[stage] || stage}」?`)) return;
        try {
            const r = await api('/reprocess', {
                method: 'POST',
                body: JSON.stringify({ selector: sel, stage, note }),
            });
            alert(`已启动重跑(${r.count} 张),可去「运行历史」看进度。`);
            // 跳转 runs 页
            document.querySelector('.tab[data-page="runs"]').click();
        } catch (err) {
            alert('启动失败:' + err.message);
        }
    }
});

// ---- Browse ----
async function refreshThumbs() {
    // 最近导入的 200 张(按 photos.created_at DESC 排,对应"刚跑完 vision 入库的"),
    // 不是按拍照时间排 — 主线扫描按拍照 ASC 顺序入,新进库的可能是几年前老照片
    const [r, status] = await Promise.all([
        api('/photos?page=0&page_size=200&order_by=imported'),
        api('/status').catch(() => ({})),
    ]);
    const total = (status.global && status.global.photos_total) || r.total || 0;
    const header = document.getElementById('browse-header');
    if (header) {
        header.textContent =
            `最近导入的 ${r.items.length} 张照片 · 图片库共 ${total.toLocaleString()} 张已扫描完成`;
    }
    const grid = document.getElementById('thumb-grid');
    grid.innerHTML = '';
    r.items.forEach(item => {
        const id = item.identity.photo_id;
        const d = document.createElement('div');
        d.className = 'thumb';
        d.innerHTML = `
            <img src="/api/thumb/${id}" loading="lazy" onerror="this.style.background='#eee'">
            <div class="cap">${item.identity.original_path.split('/').pop()}</div>
        `;
        d.onclick = () => showDetail(id);
        grid.appendChild(d);
    });
    if (r.items.length === 0) {
        grid.innerHTML = '<p class="hint">还没有照片。先在「数据源」添加目录，然后到「扫描」页扫一次。</p>';
    }
}

async function showDetail(id) {
    const rec = await api('/photo/' + encodeURIComponent(id));
    const d = document.getElementById('photo-detail');
    d.classList.remove('hidden');
    const mismatches = rec.role_mismatches || [];
    const mismatchBanner = mismatches.length > 0 ? `
        <div class="role-mismatch-banner">
            <b>提示:语义对齐度差</b>(${mismatches.length} 条) —
            struct 给的人物 action 和 description 的叙事在邻域里**字面**对不上。
            可能是同义/不同侧重的表达(LLM 风格差异,description 仍正确),
            也可能是真错位 — 请人工核对图片确认。
            <ul>${mismatches.map(m => `<li>${escapeHtml(m)}</li>`).join('')}</ul>
            <button id="ack-mismatch-btn" data-photo-id="${id}">已核对,忽略此提示</button>
        </div>` : '';
    d.innerHTML = `
        <img src="/api/thumb/${id}" title="1024px 预览(非原图),要看 / 下载原图请用下方按钮">
        <div class="detail-img-note">↑ 1024px 预览 · 右键保存的是缩略图,真原图请走下方按钮</div>
        <div class="detail-actions">
            <a href="/api/original/${encodeURIComponent(id)}?download=1" download>下载原图</a>
        </div>
        <h3>${rec.identity.original_path.split('/').pop()}</h3>
        ${mismatchBanner}
        <pre>${JSON.stringify(rec, null, 2)}</pre>
    `;
    const ackBtn = d.querySelector('#ack-mismatch-btn');
    if (ackBtn) {
        ackBtn.onclick = async () => {
            try {
                await api(`/photo/${encodeURIComponent(id)}/mismatches/acknowledge`, { method: 'POST' });
                showDetail(id);   // 重新渲染,banner 消失
            } catch (e) {
                alert('失败:' + e.message);
            }
        };
    }
    d.scrollIntoView({ behavior: 'smooth' });
}

// ---- Photo Viewer modal(chat / Browse / 未引用区点缩略图共用)----
// 设计要点(plan):
//   - 主图 src = /api/thumb/{id}(1024px JPEG),不是原图 — HEIC 浏览器不能直接渲染,
//     原图几十 MB 加载慢。三重提示用户右键保存的是缩略图(title + 灰字 note + 按钮分流)
//   - 两个原图按钮共用 /api/original/{id} 同一文件源,区别只在 ?download=1 头部
//   - "完整详情 →" 跳 Browse + showDetail(保留旧行为作 opt-in)
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
        const time = (rec.exif && (rec.exif.captured_at_local || rec.exif.captured_at_utc)) || '(时间未知)';
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

    detailBtn.onclick = () => {
        closeViewer();
        document.querySelector('.tab[data-page="browse"]').click();
        setTimeout(() => showDetail(id), 200);
    };
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

// ---- Faces ----
async function refreshClusters() {
    await Promise.all([refreshSeeds(), refreshAnonClusters()]);
}

// 回答下方追加 LLM 没引用但 search 召回的其他候选,默认折叠
function appendUncitedPhotos(parentDiv, photoIds) {
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

// cluster_id 前缀 → source 来源徽标。cluster_id 形如:
//   'apple:<name>'        — Apple Photos 已命名(在 Photos.app 里给脸贴过名字)
//   'apple_face:<uuid>'   — Apple 检测到脸但你没在 Photos.app 命名过
//   'seed_<uuid>'         — 用户上传种子图(InsightFace 锚点)
//   'c_<hash>'            — InsightFace 自动聚类的匿名 cluster
function sourceBadge(clusterId) {
    if (!clusterId) return '';
    if (clusterId.startsWith('apple_face:')) return '<span class="source-badge source-apple-anon">Apple 待命名</span>';
    if (clusterId.startsWith('apple:'))      return '<span class="source-badge source-apple">Apple Photos</span>';
    if (clusterId.startsWith('seed_'))       return '<span class="source-badge source-seed">种子面孔</span>';
    if (clusterId.startsWith('c_'))          return '<span class="source-badge source-insightface">本地识别</span>';
    return '';
}

// 已命名面孔(种子 + 已命名分组统一展示)
async function refreshSeeds() {
    const { persons } = await api('/persons');
    const list = document.getElementById('seed-list');
    list.innerHTML = '';
    if (persons.length === 0) {
        list.innerHTML = '<p class="hint">还没有已命名面孔。点右上「添加面孔」上传种子照片,或下面从未命名面孔里给一张脸起名。</p>';
        return;
    }
    persons.forEach(p => {
        const card = document.createElement('div');
        card.className = 'cluster-card';
        const seed = p.seed_count || 0;
        const total = p.total_face_count || 0;
        const main = total - seed;
        card.innerHTML = `
            <div class="head">
                <span class="person-name">${p.name}${sourceBadge(p.cluster_id)}</span>
                <span>种子 ${seed} · 主库 ${main} · <code>${p.cluster_id}</code></span>
            </div>
            <div class="samples">
                ${p.sample_face_ids.map(fid => `<img src="/api/face/${fid}/crop" loading="lazy">`).join('')}
            </div>
            <div class="name-form">
                <button class="primary add-more">追加种子</button>
                <button class="del-seed">删除面孔</button>
            </div>
        `;
        card.querySelector('.add-more').onclick = () => openSeedForm(p.name, p.cluster_id);
        card.querySelector('.del-seed').onclick = async () => {
            if (!confirm(`确定删除「${p.name}」?种子图会删,扫描产生的脸会回到未命名分组。`)) return;
            await api('/seed-persons/' + encodeURIComponent(p.cluster_id), { method: 'DELETE' });
            refreshClusters();
        };
        list.appendChild(card);
    });
}

// 匿名 cluster
async function refreshAnonClusters() {
    const { clusters } = await api('/face-clusters');
    const list = document.getElementById('cluster-list');
    list.innerHTML = '';
    if (clusters.length === 0) {
        list.innerHTML = '<p class="hint">没有未命名的面孔分组。要么还没扫描过,要么所有脸都被已命名面孔匹配上了。</p>';
        return;
    }
    clusters.forEach(c => {
        const card = document.createElement('div');
        card.className = 'cluster-card';
        // apple_face: 是 Apple Photos 检测到但没在 Photos.app 命名的脸,在我们这里命名只对单张照片有效
        // (Apple FaceInfo.uuid 每张脸独立,不跨照片聚类)→ 改成显示引导文字,不让用户在这里输入
        const isAppleAnon = c.cluster_id.startsWith('apple_face:');
        const formHtml = isAppleAnon
            ? `<p class="hint" style="margin:8px 0 0;font-size:12px">
                 这是 Apple Photos 检测到的脸,但你在「照片」应用里还没给它命名。<br>
                 请到 macOS「照片」App 里命名这个人,系统会把同一个人在所有照片里的脸聚类。
                 之后这里重新扫描或重跑 faces,会自动出现一张已命名的 <b>Apple Photos</b> 卡。
               </p>`
            : `<div class="name-form">
                 <input type="text" placeholder="给这个人起个名字">
                 <button class="primary">保存</button>
               </div>`;
        card.innerHTML = `
            <div class="head">
                <span class="person-name unnamed">未命名${sourceBadge(c.cluster_id)}</span>
                <span>${c.face_count} 张 · <code>${c.cluster_id}</code></span>
            </div>
            <div class="samples">
                ${c.sample_face_ids.map(fid => `<img src="/api/face/${fid}/crop" loading="lazy">`).join('')}
            </div>
            ${formHtml}
        `;
        if (isAppleAnon) {
            list.appendChild(card);
            return;
        }
        const input = card.querySelector('input');
        const btn = card.querySelector('button');
        btn.onclick = async () => {
            const name = input.value.trim();
            if (!name) return;
            await api('/persons/' + encodeURIComponent(c.cluster_id) + '/name', {
                method: 'POST',
                body: JSON.stringify({ name }),
            });
            refreshClusters();
        };
        input.addEventListener('keydown', e => { if (e.key === 'Enter') btn.click(); });
        list.appendChild(card);
    });
}

// 种子表单
function openSeedForm(prefillName = '', extendClusterId = null) {
    document.getElementById('seed-form').classList.remove('hidden');
    document.getElementById('seed-form-title').textContent = extendClusterId
        ? `追加照片 — ${prefillName}`
        : '添加面孔';
    const nameInput = document.getElementById('seed-name');
    nameInput.value = prefillName;
    nameInput.disabled = !!extendClusterId;   // 追加时不允许改名
    document.getElementById('seed-files').value = '';
    document.getElementById('seed-form-status').textContent = '';
}

function closeSeedForm() {
    document.getElementById('seed-form').classList.add('hidden');
}

document.getElementById('add-seed-btn').addEventListener('click', () => openSeedForm());
document.getElementById('seed-cancel').addEventListener('click', closeSeedForm);

document.getElementById('seed-submit').addEventListener('click', async () => {
    const name = document.getElementById('seed-name').value.trim();
    const filesInput = document.getElementById('seed-files');
    const files = filesInput.files;
    const status = document.getElementById('seed-form-status');
    if (!name) { status.textContent = '请填姓名'; return; }
    if (!files || files.length === 0) { status.textContent = '请选至少 1 张照片'; return; }

    status.textContent = `上传 ${files.length} 张...`;
    const fd = new FormData();
    fd.append('name', name);
    for (const f of files) fd.append('files', f);
    try {
        const r = await fetch('/api/seed-persons', { method: 'POST', body: fd });
        if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
        const data = await r.json();
        let msg = `已添加 ${data.added} 张到「${data.name}」`;
        if (data.rematch && data.rematch.ok) {
            const m = data.rematch;
            if (m.anchors === 0) {
                msg += `;暂无锚点,跳过自动匹配`;
            } else {
                msg += `;已把 ${m.moved}/${m.candidates} 张主库的脸归到这个面孔`;
            }
        }
        if (data.warnings && data.warnings.length) {
            msg += `;警告: ${data.warnings.join('; ')}`;
        }
        status.textContent = msg;
        refreshClusters();

        // 如果有受影响照片,提议刷新描述
        const affected = (data.rematch && data.rematch.affected_photo_ids) || [];
        if (affected.length > 0) {
            const seconds = Math.round(affected.length * 20);
            if (confirm(`${affected.length} 张照片的脸归到了「${data.name}」,要刷新这些照片的描述让 LLM 用上姓名吗?\n\n约需 ${seconds} 秒。可以稍后再刷,不阻塞使用。`)) {
                status.textContent = `刷新描述 中... ${affected.length} 张,预计 ${seconds} 秒`;
                try {
                    const r2 = await api('/reprocess', {
                        method: 'POST',
                        body: JSON.stringify({ group: 'vision', photo_ids: affected }),
                    });
                    status.textContent = `描述已刷新:${r2.done}/${r2.requested} 张成功`;
                } catch (e2) {
                    status.textContent = '刷新描述 失败: ' + e2.message;
                }
            }
        }
        setTimeout(closeSeedForm, 3500);
    } catch (err) {
        status.textContent = '失败: ' + err.message;
    }
});

// ---- Status ----
async function refreshStatus() {
    const s = await api('/status');
    document.getElementById('status-json').textContent = JSON.stringify(s, null, 2);
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

// (escapeHtml 已在 settings tab routing 区域定义)

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

function renderAssistantBody(div, text, candidateIds) {
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
                    if (metaDiv.dataset.gotPlan) {
                        // 更新 summary 字段不动其他
                        const sumEl = metaDiv.querySelector('.chat-trace-summary');
                        if (sumEl) sumEl.textContent = parsed.summary;
                    } else {
                        metaDiv.textContent = `${parsed.summary} · 生成回答中...`;
                    }
                } else if (evt === 'chunk') {
                    buf += parsed;
                    renderAssistantBody(respDiv, buf, candidateIds);
                    chatLog.scrollTop = chatLog.scrollHeight;
                } else if (evt === 'done') {
                    // 在回答下方追加"未被回答引用的其他候选"折叠区
                    const citedIds = new Set(parsed.photo_ids || []);
                    const uncited = toolResultItems
                        .map(it => it.photo_id)
                        .filter(pid => pid && !citedIds.has(pid));
                    if (uncited.length > 0) {
                        appendUncitedPhotos(respDiv, uncited);
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

// 启动时 init() IIFE 已经自动激活合适 tab,不再需要这里 refresh
// (旧代码:refreshSources())
