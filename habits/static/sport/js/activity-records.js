/* Okno „Rekordy”: wszystkie rekordy naraz (bieganie, pływanie, każde
 * ćwiczenie na siłowni) z historią - od pierwszego wyniku przez każde
 * pobicie, z łączną poprawą.
 *
 * window.ActivityRecords.open(treningi, ctx), gdzie ctx = { onOpenWorkout(uid) }.
 * Dane liczy ActivityStats.recordHistory (board).
 */
(function () {
    'use strict';

    var S = window.ActivityStats;
    var modalNode = document.getElementById('act-records-modal');
    if (!S || !modalNode) { return; }

    var context = null;
    var SECTIONS = [['run', 'Bieganie'], ['swim', 'Pływanie'], ['gym', 'Siłownia']];

    function esc(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function dayButton(uid, date) {
        return '<button type="button" class="act-records-day" data-uid="' + esc(uid) + '">' + esc(S.dayLabel(date)) + '</button>';
    }

    function historyHtml(record) {
        var rows = ['<li><span class="act-records-step">' + esc(record.first.value) + '</span>' +
            '<span class="act-records-note">pierwszy wynik · ' + dayButton(record.first.uid, record.first.date) + '</span></li>'];
        record.beats.forEach(function (event) {
            rows.push('<li><span class="act-records-step">' + esc(event.value) +
                ' <span class="act-record-up">' + esc(event.improvement) + '</span></span>' +
                '<span class="act-records-note">' + dayButton(event.uid, event.date) + '</span></li>');
        });
        return '<details class="act-records-history"><summary>Historia (' + record.beats.length + ' ' +
            S.plural(record.beats.length, 'pobicie', 'pobicia', 'pobić') + ')</summary><ol>' + rows.join('') + '</ol></details>';
    }

    function recordHtml(record) {
        var beats = record.beats.length;
        var summary = beats
            ? 'Łącznie <span class="act-record-up">' + esc(record.total) + '</span> od ' + esc(record.first.value)
            : 'Pierwszy wynik – jeszcze niepobity';
        return '<div class="act-records-item">' +
            '<div class="act-records-head"><span class="act-record-label">' + esc(record.label) + '</span>' +
            dayButton(record.uid, record.date) + '</div>' +
            '<div class="act-record-value">' + esc(record.value) + '</div>' +
            '<div class="act-record-detail">' + summary + '</div>' +
            (beats ? historyHtml(record) : '') + '</div>';
    }

    function render(workouts) {
        var board = S.recordHistory(workouts).board;
        var body = SECTIONS.map(function (section) {
            var records = board.filter(function (record) { return record.kind === section[0]; });
            if (!records.length) { return ''; }
            return '<section class="act-records-section">' +
                '<h3 class="act-detail-subtitle"><span class="act-day-dot act-k-' + section[0] + '"></span> ' +
                esc(section[1]) + ' <span class="act-records-count">' + records.length + '</span></h3>' +
                '<div class="act-records-grid">' + records.map(recordHtml).join('') + '</div></section>';
        }).join('');
        modalNode.querySelector('[data-act-records-body]').innerHTML = body ||
            '<p class="act-empty">Rekordy pojawią się po pierwszych treningach z dystansem albo ciężarem.</p>';
    }

    function open(workouts, ctx) {
        context = ctx;
        render(workouts);
        var modal = window.bootstrap ? bootstrap.Modal.getOrCreateInstance(modalNode) : null;
        if (modal) { modal.show(); }
    }

    // Kliknięta data otwiera trening - dopiero po zamknięciu tego okna,
    // bo dwa okna Bootstrapa naraz się gryzą.
    modalNode.addEventListener('click', function (event) {
        var button = event.target.closest('[data-uid]');
        if (!button || !context) { return; }
        var uid = button.getAttribute('data-uid');
        var modal = window.bootstrap ? bootstrap.Modal.getOrCreateInstance(modalNode) : null;
        if (!modal) { context.onOpenWorkout(uid); return; }
        modalNode.addEventListener('hidden.bs.modal', function once() {
            modalNode.removeEventListener('hidden.bs.modal', once);
            context.onOpenWorkout(uid);
        });
        modal.hide();
    });

    window.ActivityRecords = { open: open };
})();
