/* ========================================================================
   SOFT REVEAL - fade + lift content in as it scrolls into view.
   Site-wide, no per-page markup needed: the script tags the elements itself
   (class "reveal-target", which soft-reveal.css hides) and reveals them with a
   small stagger once they enter the viewport.

   Deliberately left alone:
     - the premium home (.ph-page) and landing page (.pl-doc), which already
       animate themselves,
     - modals, dropdowns and the split view's injected tool forms, where a
       hidden-until-observed element could be a live control,
     - anything fixed (header, sidebar, toasts, background layers), since a
       transform on an ancestor would break fixed positioning.
   ======================================================================== */

(function () {
    var REVEAL_SELECTOR = [
        '.ds-card',            // dashboard panels
        '.dashboard-header',
        '.section-title',
        '.main-content .card', // every card-based page (settings, files, logs, tools)
        '.batch-header',       // batches page, which uses its own blocks
        '.batch-workspace',
        '.search-box',
        '.tg-page-header',     // Tango
        '.tg-card'
    ].join(',');

    var SKIP_INSIDE = [
        '.modal',
        '.dropdown-menu',
        '#splitView',
        '.ph-page',
        '.pl-doc',
        '.flash-toasts',
        '#loading-screen'
    ].join(',');

    var STAGGER_MS = 70;
    var MAX_STAGGER_MS = 350;

    var observer = null;
    var pending = [];

    function isEligible(el) {
        if (el.classList.contains('reveal-target')) return false;
        if (el.closest(SKIP_INSIDE)) return false;
        // A transform on an element containing fixed children would re-root
        // their positioning, so leave those alone.
        if (el.querySelector('.ds-sidebar, .simple-header')) return false;
        return true;
    }

    function reveal(el, index) {
        var delay = Math.min(index * STAGGER_MS, MAX_STAGGER_MS);
        el.style.setProperty('--reveal-delay', delay + 'ms');
        el.classList.add('is-revealed');
    }

    function onIntersect(entries) {
        // Reveal in document order so a row of cards cascades left to right.
        var visible = entries.filter(function (entry) { return entry.isIntersecting; });
        visible.sort(function (a, b) {
            return a.boundingClientRect.top - b.boundingClientRect.top ||
                   a.boundingClientRect.left - b.boundingClientRect.left;
        });
        visible.forEach(function (entry, i) {
            reveal(entry.target, i);
            observer.unobserve(entry.target);
        });
    }

    function scan(root) {
        var scope = root && root.querySelectorAll ? root : document;
        var found = scope.querySelectorAll(REVEAL_SELECTOR);
        Array.prototype.forEach.call(found, function (el) {
            if (!isEligible(el)) return;
            el.classList.add('reveal-target');
            if (observer) {
                observer.observe(el);
            } else {
                pending.push(el);
            }
        });
    }

    function revealEverything() {
        var all = document.querySelectorAll('.reveal-target:not(.is-revealed)');
        Array.prototype.forEach.call(all, function (el, i) { reveal(el, i); });
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!document.documentElement.classList.contains('js-reveal')) return;

        if (!('IntersectionObserver' in window)) {
            scan(document);
            revealEverything();
            window.__softRevealReady = true;
            return;
        }

        observer = new IntersectionObserver(onIntersect, {
            // Any sliver on screen counts. A percentage threshold would leave a
            // card taller than the viewport hidden forever, since it could never
            // show that share of itself.
            rootMargin: '0px 0px -40px 0px',
            threshold: 0
        });

        scan(document);
        pending.forEach(function (el) { observer.observe(el); });
        pending = [];

        // Content injected later (the split view's tool panel, expanded user
        // tables) gets picked up too.
        if ('MutationObserver' in window) {
            var queued = false;
            new MutationObserver(function (mutations) {
                if (queued) return;
                var hasNodes = mutations.some(function (m) { return m.addedNodes.length; });
                if (!hasNodes) return;
                queued = true;
                requestAnimationFrame(function () {
                    queued = false;
                    scan(document);
                });
            }).observe(document.body, { childList: true, subtree: true });
        }

        window.__softRevealReady = true;
    });
})();
