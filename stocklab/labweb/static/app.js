/* ============================================================
   stock-lab · 台账前端增强
   ------------------------------------------------------------
   渐进增强：**没有它页面照样能用**。所有写操作的原生路径
   （POST → 303 → GET）原封不动保留；本文件只是把它换成
   局部更新，省掉一次整页白屏。

   三条纪律
   1. 只在「确定能本地处理」时才拦表单；拿不准就 form.submit()
      交还给浏览器 —— 半路失败的 fetch 比整页刷新糟得多。
   2. 服务端说的话原样显示。前端不重写、不美化错误文本。
   3. 不猜。查不到字段容器就不标红，只显示横幅。
   ============================================================ */
(function () {
  'use strict';

  var reduce = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- 小工具 ---------- */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  // 服务端回的是 HTML 片段，不是 JSON —— 用 <template> 解析，避免 innerHTML 触发脚本
  function html(str) {
    var t = document.createElement('template');
    t.innerHTML = (str || '').trim();
    return t.content;
  }

  function replaceNode(sel, markup) {
    var node = $(sel);
    if (!node || !markup) { return null; }
    var frag = html(markup);
    var next = frag.firstElementChild;
    if (!next) { return null; }
    node.replaceWith(next);
    return next;
  }

  function setBusy(form, busy) {
    var btn = $('button[type="submit"]', form);
    if (!btn) { return; }
    if (busy) {
      if (!btn.dataset.label) { btn.dataset.label = btn.textContent; }
      btn.disabled = true;
      btn.textContent = '提交中…';
    } else {
      btn.disabled = false;
      btn.textContent = btn.dataset.label || btn.textContent;
    }
  }

  function clearErrors(form) {
    $$('.field', form).forEach(function (f) {
      f.classList.remove('bad');
      var e = $('.err', f);
      if (e) { e.textContent = ''; }
    });
    var b = $('.form-error', form);
    if (b) { b.remove(); }
  }

  // 服务端给了字段名就标在字段旁；没给就只出横幅 —— 不猜是哪一格
  function showError(form, message, field) {
    var banner = document.createElement('p');
    banner.className = 'alert s-fail form-error';
    banner.setAttribute('role', 'alert');
    banner.textContent = message;
    form.insertBefore(banner, form.firstChild);
    if (!field) { return; }
    var box = $('.field[data-field="' + field + '"]', form);
    if (!box) { return; }
    box.classList.add('bad');
    var err = $('.err', box);
    if (err) { err.textContent = message; }
    var input = $('input, select', box);
    if (input) { input.focus(); }
  }

  /* ---------- 客户端预检（只是省一次往返，服务端仍会再校验一遍） ---------- */

  function localCheck(form) {
    var bad = null;
    var side = form.elements.side;
    var qty = form.elements.qty;
    if (side && qty && side.value === 'buy' && qty.value) {
      var n = Number(qty.value);
      if (isFinite(n) && n > 0 && n % 100 !== 0) {
        bad = { field: 'qty', message: '买入数量必须是 100 股整数倍，收到 ' + qty.value };
      }
    }
    var price = form.elements.price;
    if (!bad && price && price.value) {
      var p = Number(price.value);
      if (isFinite(p) && p <= 0) {
        bad = { field: 'price', message: '价格必须大于 0，收到 ' + price.value };
      }
    }
    if (bad) { clearErrors(form); showError(form, bad.message, bad.field); }
    return bad;
  }

  /* ---------- 局部提交 ---------- */

  function submitFragment(form) {
    var action = form.getAttribute('action');
    var body = new URLSearchParams(new FormData(form));
    setBusy(form, true);

    fetch(action, {
      method: 'POST',
      body: body,
      credentials: 'same-origin',
      headers: { 'X-Lab-Fragment': '1' }
    }).then(function (res) {
      var ctype = res.headers.get('content-type') || '';
      if (ctype.indexOf('application/json') !== 0) {
        // 403（令牌）/ 409（疑似重复）回的是整页 —— 那是有意义的页面，
        // 硬塞进 DOM 只会更差。交回浏览器去渲染。
        return handBack(form);
      }
      return res.json().then(function (data) {
        if (data && data.ok) { render(form, data); }
        else { fail(form, data); }
      });
    }).catch(function () {
      handBack(form);          // 网络断了：退回原生提交，别让人白填一遍
    });
  }

  function fail(form, data) {
    setBusy(form, false);
    clearErrors(form);
    showError(form, (data && data.error) || '提交失败', data && data.field);
  }

  function handBack(form) {
    setBusy(form, false);
    form.removeAttribute('data-fragment');
    form.submit();             // 绕过本监听器，走浏览器原生 POST
  }

  function render(form, data) {
    var target = form.getAttribute('data-pane');
    var receipt = replaceNode(target, data.pane_html);

    if (data.receipt_html) {
      var box = replaceNode('#receipt', data.receipt_html);
      if (box && !reduce && box.classList) { box.classList.add('landed'); }
    }
    clearErrors(form);
    form.reset();
    setBusy(form, false);
    if (data.url) { history.replaceState(null, '', data.url); }

    // 焦点交给回执：读屏用户要立刻知道「写成了什么」
    var live = $('#receipt');
    if (live) {
      live.setAttribute('role', 'status');
      live.setAttribute('tabindex', '-1');
      live.focus({ preventScroll: true });
    } else if (receipt) {
      receipt.scrollIntoView({ block: 'nearest' });
    }
  }

  /* ---------- 绑定 ---------- */

  function wireForm(form) {
    if (!form.hasAttribute('data-fragment') || !window.fetch) { return; }

    form.addEventListener('submit', function (ev) {
      if (ev.defaultPrevented) { return; }
      if (form.elements.confirm_duplicate) { return; }  // 确认页保持原生提交
      if (localCheck(form)) { ev.preventDefault(); return; }
      ev.preventDefault();
      submitFragment(form);
    });

    // 失焦时预检一次：错误出现在字段旁，不用等到按提交
    $$('input, select', form).forEach(function (el) {
      el.addEventListener('blur', function () {
        if (form.elements.side && form.elements.qty) { localCheck(form); }
      });
    });
  }

  /* ---------- 不可逆动作：原生 <dialog> 确认 ---------- */

  function wireConfirm(form) {
    var text = form.getAttribute('data-confirm');
    if (!text) { return; }
    var dlg = document.createElement('dialog');
    dlg.className = 'confirm';
    var f = document.createElement('form');
    f.method = 'dialog';                       // Esc 与「取消」都走这条
    f.innerHTML =
      '<h2 class="confirm__h"></h2><div class="confirm__b"></div>' +
      '<div class="confirm__f"><button class="btn ghost" value="cancel">取消</button>' +
      '<button class="btn danger" value="go"></button></div>';
    dlg.appendChild(f);
    document.body.appendChild(dlg);
    $('.confirm__h', dlg).textContent = text;
    $('.confirm__b', dlg).textContent =
      form.getAttribute('data-confirm-detail') || '';
    $('.btn.danger', dlg).textContent =
      form.getAttribute('data-confirm-ok') || '确认';

    if (!window.HTMLDialogElement) {           // 老浏览器：退回系统 confirm
      form.addEventListener('submit', function (ev) {
        if (!window.confirm(text)) { ev.preventDefault(); }
      });
      return;
    }

    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      dlg.returnValue = '';
      dlg.showModal();
      dlg.addEventListener('close', function once() {
        dlg.removeEventListener('close', once);
        if (dlg.returnValue === 'go') {
          form.removeAttribute('data-confirm');
          form.submit();
        }
      });
    });
  }

  function init() {
    $$('form[data-fragment]').forEach(wireForm);
    $$('form[data-confirm]').forEach(wireConfirm);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
