/* ========================================================================
   ATTENDANCEIQ — PREMIUM HOME interactions
   Per-card glass reflection tracking.
   Scoped entirely to elements that only exist on the premium home page,
   so this file is a no-op anywhere else.
   ======================================================================== */

document.addEventListener('DOMContentLoaded', function () {
    var page = document.getElementById('phPage');
    if (!page) return;

    // Per-card glass reflection: track cursor position relative to each card.
    var cards = page.querySelectorAll('.ph-tool-card, .ph-feature-card');
    cards.forEach(function (card) {
        card.addEventListener('mousemove', function (e) {
            var rect = card.getBoundingClientRect();
            var mx = ((e.clientX - rect.left) / rect.width) * 100;
            var my = ((e.clientY - rect.top) / rect.height) * 100;
            card.style.setProperty('--mx', mx + '%');
            card.style.setProperty('--my', my + '%');
        });
    });
});
