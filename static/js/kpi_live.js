/* Read saved application data only. Never initiate an Xpert or terminal import. */
(function () {
    'use strict';

    function init() {
        var root = document.querySelector('[data-kpi-root]');
        if (!root || !window.XMLHttpRequest) return;
        var panel = root.querySelector('[data-kpi-panel]');
        var cards = root.querySelector('[data-kpi-cards]');
        var readTime = root.querySelector('[data-kpi-read]');
        var error = root.querySelector('[data-kpi-error]');
        var interval = Math.max(10, Number(root.dataset.kpiInterval) || 30) * 1000;
        var timer = null;
        var request = null;
        var stopped = false;

        function scheduleNext() {
            window.clearTimeout(timer);
            if (!stopped) timer = window.setTimeout(refresh, interval);
        }

        function tableRows(table) {
            var $ = window.jQuery;
            if ($ && $.fn.DataTable && $.fn.DataTable.isDataTable(table)) {
                var api = $(table).DataTable();
                return {nodes: api.rows().nodes().toArray(), api: api};
            }
            return {nodes: Array.prototype.slice.call(table.querySelectorAll('[data-live-task-id]')), api: null};
        }

        function updateText(row, selector, value) {
            var element = row.querySelector(selector);
            var text = String(value == null ? '—' : value);
            if (!element || element.textContent === text) return false;
            element.textContent = text;
            return true;
        }

        function updateTasks(data) {
            if (root.dataset.kpiSurface !== 'live') return;
            var notice = document.querySelector('[data-live-assignment-change]');
            var table = document.querySelector('[data-live-task-table]');
            var originalSchedule = root.dataset.kpiSchedule || '';
            var newSchedule = data.schedule_id == null ? '' : String(data.schedule_id);
            var changed = originalSchedule !== newSchedule;
            var tasks = Array.isArray(data.tasks) ? data.tasks : [];
            if (!table) {
                if (notice) notice.hidden = !changed && tasks.length === 0;
                return;
            }
            var rows = tableRows(table);
            var byId = {};
            rows.nodes.forEach(function (row) { byId[row.dataset.liveTaskId] = row; });
            if (rows.nodes.length !== tasks.length) changed = true;
            var updated = false;
            tasks.forEach(function (task) {
                var row = byId[String(task.id)];
                if (!row) { changed = true; return; }
                if (updateText(row, '[data-live-task-name]', task.name)) updated = true;
                if (updateText(row, '[data-live-task-priority]', task.priority)) updated = true;
                if (updateText(row, '[data-live-task-goal]', task.goal)) updated = true;
                if (updateText(row, '[data-live-task-rate]', task.rate_label)) updated = true;
                var bar = row.querySelector('[data-live-task-bar]');
                if (bar) {
                    var numeric = Number(task.bar_value);
                    var barValue = Number.isFinite(numeric) ? Math.max(0, Math.min(100, numeric)) : 0;
                    if (Number(bar.value) !== barValue) { bar.value = barValue; updated = true; }
                    bar.setAttribute('aria-valuetext', String(task.rate_label || '—'));
                    bar.setAttribute('aria-label', 'Progression : ' + String(task.name || ''));
                }
            });
            // A change in schedule/assignment set needs a deliberate page reload;
            // do not destroy an open form or modal to insert or remove rows.
            if (notice) notice.hidden = !changed;
            if (updated && rows.api) rows.api.rows().invalidate('dom').draw(false);
        }

        function success(data) {
            if (!data || typeof data.html !== 'string' || !Array.isArray(data.tasks)) throw new Error('Invalid snapshot');
            if (cards.innerHTML !== data.html) cards.innerHTML = data.html;
            if (readTime && typeof data.read_at === 'string') readTime.textContent = data.read_at;
            if (error) error.hidden = true;
            if (panel && root.dataset.kpiSurface === 'live') panel.hidden = !data.card_count;
            updateTasks(data);
        }

        function failed() {
            if (error) error.hidden = false;
        }

        function refresh() {
            window.clearTimeout(timer);
            if (stopped || request) return;
            if (document.visibilityState === 'hidden') { scheduleNext(); return; }
            var url = root.dataset.kpiUrl + '?surface=' + encodeURIComponent(root.dataset.kpiSurface);
            // A live home tab should follow the current month across midnight.
            if (root.dataset.kpiSurface === 'dashboard') url += '&month=' + encodeURIComponent(root.dataset.kpiMonth);
            var xhr = new window.XMLHttpRequest();
            request = xhr;
            xhr.open('GET', url, true);
            xhr.timeout = 15000;
            xhr.setRequestHeader('Accept', 'application/json');
            xhr.onload = function () {
                request = null;
                try {
                    if (xhr.status !== 200 || !(xhr.getResponseHeader('Content-Type') || '').includes('application/json')) throw new Error('Unavailable');
                    success(JSON.parse(xhr.responseText));
                } catch (_err) { failed(); }
                scheduleNext();
            };
            xhr.onerror = xhr.ontimeout = function () { request = null; failed(); scheduleNext(); };
            xhr.onabort = function () { request = null; };
            xhr.send();
        }

        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState !== 'hidden' && !stopped) refresh();
        });
        window.addEventListener('pagehide', function () {
            stopped = true;
            window.clearTimeout(timer);
            if (request) request.abort();
        });
        window.addEventListener('pageshow', function (event) {
            if (event.persisted) { stopped = false; refresh(); }
        });
        scheduleNext();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
}());
