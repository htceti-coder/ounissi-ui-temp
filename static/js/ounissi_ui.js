/*
 * OUNISSI OS v2 — progressive UI controller
 * ---------------------------------------------------------------
 * IMPORTANT: this file does not replace feature logic. It preserves:
 *   - create_schedule.js / create_schedule_DATE.js
 *   - salary_form.js / js_utils.js
 *   - inline template AJAX, Bootstrap modal, Chosen and DataTables logic
 * Existing IDs, names, data attributes, onclick handlers and endpoints are
 * never renamed or removed.
 */
(function (window, document, $) {
    "use strict";

    var UI = window.OunissiUI = window.OunissiUI || {};
    var root = document.documentElement;
    var body = document.body;
    var storagePrefix = "ounissi.ui.";
    var activeCommandIndex = 0;
    var commandItems = [];

    function ready(callback) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", callback, { once: true });
        } else {
            callback();
        }
    }

    function safeStorageGet(key) {
        try { return window.localStorage.getItem(storagePrefix + key); }
        catch (error) { return null; }
    }

    function safeStorageSet(key, value) {
        try { window.localStorage.setItem(storagePrefix + key, value); }
        catch (error) { /* private browsing/storage disabled */ }
    }

    function select(selector, scope) {
        return (scope || document).querySelector(selector);
    }

    function selectAll(selector, scope) {
        return Array.prototype.slice.call((scope || document).querySelectorAll(selector));
    }

    function debounce(callback, wait) {
        var timeout;
        return function () {
            var context = this;
            var args = arguments;
            window.clearTimeout(timeout);
            timeout = window.setTimeout(function () { callback.apply(context, args); }, wait);
        };
    }

    /* ------------------------------------------------------------------ */
    /* Public compatibility APIs used by existing templates/scripts       */
    /* ------------------------------------------------------------------ */

    window.w3_open = function () {
        body.classList.add("sidebar-open");
        var sidebar = select("#mySidebar");
        var overlay = select("#sidebar-overlay");
        if (sidebar) sidebar.setAttribute("aria-hidden", "false");
        if (overlay) overlay.setAttribute("aria-hidden", "false");
    };

    window.w3_close = function () {
        body.classList.remove("sidebar-open");
        var sidebar = select("#mySidebar");
        var overlay = select("#sidebar-overlay");
        if (sidebar && window.innerWidth < 992) sidebar.setAttribute("aria-hidden", "true");
        if (overlay) overlay.setAttribute("aria-hidden", "true");
    };

    window.showLoadingContainer = function () {
        var loader = select("#loading-container");
        if (!loader) return;
        loader.classList.remove("hidden");
        loader.setAttribute("aria-hidden", "false");
    };

    window.hideLoadingContainer = function () {
        var loader = select("#loading-container");
        if (!loader) return;
        loader.classList.add("hidden");
        loader.setAttribute("aria-hidden", "true");
    };

    /* Signature kept identical to the old base templates. */
    window.showModal = function (
        title,
        message,
        button1Text,
        button2Text
    ) {
        title = title || "Message";
        message = message || "";
        button1Text = button1Text || "Fermer";
        button2Text = button2Text || "OK";

        if (!$ || !$.fn || !$.fn.modal) {
            window.alert(message);
            return;
        }

        var modal = $("#genericModal");
        if (!modal.length) {
            window.alert(message);
            return;
        }

        modal.find("#genericModalLabel").text(title);
        modal.find("#modal-message").text(message);
        modal.find("#button1").text(button1Text).attr("data-dismiss", "modal");
        modal.find("#button2").text(button2Text).attr("data-dismiss", "modal");
        modal.modal("show");
    };

    /* ------------------------------------------------------------------ */
    /* Shell                                                              */
    /* ------------------------------------------------------------------ */

    function initSidebar() {
        var openButton = select("[data-sidebar-open]");
        var closeButton = select("[data-sidebar-close]");
        var overlay = select("#sidebar-overlay");

        if (openButton) openButton.addEventListener("click", window.w3_open);
        if (closeButton) closeButton.addEventListener("click", window.w3_close);
        if (overlay) overlay.addEventListener("click", window.w3_close);

        selectAll(".app-sidebar a").forEach(function (link) {
            link.addEventListener("click", function () {
                if (window.innerWidth < 992) window.w3_close();
            });
        });

        window.addEventListener("resize", debounce(function () {
            if (window.innerWidth >= 992) window.w3_close();
        }, 100));
    }

    function normalizePath(path) {
        if (!path) return "/";
        var normalized = path.split("?")[0].replace(/\/+$/, "");
        return normalized || "/";
    }

    function initActiveNavigation() {
        var current = normalizePath(window.location.pathname);
        var bestLink = null;
        var bestLength = -1;

        selectAll(".sidebar-nav .nav-link").forEach(function (link) {
            var target;
            try { target = normalizePath(new URL(link.href, window.location.origin).pathname); }
            catch (error) { return; }

            var explicit = link.getAttribute("data-nav-match");
            var isMatch = explicit === "/" ? current === "/" : (current === target || (target !== "/" && current.indexOf(target + "/") === 0));
            if (isMatch && target.length > bestLength) {
                bestLink = link;
                bestLength = target.length;
            }
        });

        if (bestLink) {
            bestLink.classList.add("is-active");
            bestLink.setAttribute("aria-current", "page");
        }
    }

    function derivePageIdentity() {
        var topTitle = select("#app-page-title");
        var kicker = select("#page-kicker");
        var content = select("[data-page-content]");
        if (!topTitle || !content) return;

        var heading = select("h1, h2", content);
        var title = heading && heading.textContent.trim();
        if (!title) {
            title = (document.title || "OUNISSI OS").split("|")[0].trim();
        }

        topTitle.textContent = title || "Espace de travail";
        if (kicker) {
            var path = normalizePath(window.location.pathname);
            kicker.textContent = path === "/" ? "Tableau de bord" : path.replace(/^\//, "").replace(/\//g, " · ");
        }
    }

    function updateClocks() {
        var now = new Date();
        var time = now.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        var shortTime = now.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
        var date = now.toLocaleDateString("fr-FR", { weekday: "short", day: "2-digit", month: "short" });
        var topClock = select("#topbar-clock");
        var topDate = select("#topbar-date");
        var sideClock = select("#sidebar-clock");
        if (topClock) topClock.textContent = time;
        if (topDate) topDate.textContent = date;
        if (sideClock) sideClock.textContent = shortTime;
    }

    function initTheme() {
        var saved = safeStorageGet("theme");
        var preferredDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
        var theme = saved || (preferredDark ? "dark" : "light");
        applyTheme(theme);

        var toggle = select("[data-theme-toggle]");
        if (toggle) {
            toggle.addEventListener("click", function () {
                var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
                applyTheme(next);
                safeStorageSet("theme", next);
            });
        }
    }

    function applyTheme(theme) {
        root.setAttribute("data-theme", theme);
        var icon = select("[data-theme-toggle] i");
        if (icon) {
            icon.className = theme === "dark" ? "fa fa-sun-o" : "fa fa-moon-o";
        }
    }

    /* ------------------------------------------------------------------ */
    /* Command palette                                                    */
    /* ------------------------------------------------------------------ */

    function collectCommands() {
        var seen = {};
        commandItems = selectAll(".sidebar-nav .nav-link").map(function (link) {
            var label = (link.textContent || "").replace(/LIVE/g, "").trim();
            var href = link.getAttribute("href");
            if (!href || seen[href]) return null;
            seen[href] = true;
            var icon = select("i", link);
            return {
                label: label,
                href: href,
                path: normalizePath(href),
                icon: icon ? icon.className : "fa fa-link"
            };
        }).filter(Boolean);
    }

    function openCommandPalette() {
        var palette = select("#command-palette");
        var input = select("#command-input");
        if (!palette || !input) return;
        palette.classList.add("is-open");
        palette.setAttribute("aria-hidden", "false");
        body.style.overflow = "hidden";
        input.value = "";
        activeCommandIndex = 0;
        renderCommands("");
        window.setTimeout(function () { input.focus(); }, 20);
    }

    function closeCommandPalette() {
        var palette = select("#command-palette");
        if (!palette) return;
        palette.classList.remove("is-open");
        palette.setAttribute("aria-hidden", "true");
        body.style.overflow = "";
    }

    function renderCommands(query) {
        var container = select("#command-results");
        if (!container) return;
        var normalized = (query || "").toLowerCase().trim();
        var matches = commandItems.filter(function (item) {
            return !normalized || item.label.toLowerCase().indexOf(normalized) !== -1 || item.path.toLowerCase().indexOf(normalized) !== -1;
        });

        if (activeCommandIndex >= matches.length) activeCommandIndex = Math.max(0, matches.length - 1);
        container.innerHTML = "";

        if (!matches.length) {
            var empty = document.createElement("div");
            empty.className = "command-empty";
            empty.textContent = "Aucune page ne correspond à cette recherche.";
            container.appendChild(empty);
            return;
        }

        matches.forEach(function (item, index) {
            var link = document.createElement("a");
            link.href = item.href;
            link.className = "command-result" + (index === activeCommandIndex ? " is-selected" : "");
            link.setAttribute("data-command-index", String(index));

            var icon = document.createElement("i");
            icon.className = item.icon;
            var label = document.createElement("span");
            label.textContent = item.label;
            var path = document.createElement("small");
            path.textContent = item.path;
            link.appendChild(icon);
            link.appendChild(label);
            link.appendChild(path);
            container.appendChild(link);
        });
    }

    function moveCommandSelection(direction) {
        var results = selectAll("#command-results .command-result");
        if (!results.length) return;
        activeCommandIndex = (activeCommandIndex + direction + results.length) % results.length;
        results.forEach(function (result, index) {
            result.classList.toggle("is-selected", index === activeCommandIndex);
        });
        results[activeCommandIndex].scrollIntoView({ block: "nearest" });
    }

    function initCommandPalette() {
        collectCommands();
        var input = select("#command-input");
        selectAll("[data-command-open]").forEach(function (button) { button.addEventListener("click", openCommandPalette); });
        selectAll("[data-command-close]").forEach(function (button) { button.addEventListener("click", closeCommandPalette); });
        if (input) input.addEventListener("input", function () { activeCommandIndex = 0; renderCommands(input.value); });

        document.addEventListener("keydown", function (event) {
            var palette = select("#command-palette");
            var open = palette && palette.classList.contains("is-open");
            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
                event.preventDefault();
                open ? closeCommandPalette() : openCommandPalette();
                return;
            }
            if (!open) return;
            if (event.key === "Escape") closeCommandPalette();
            if (event.key === "ArrowDown") { event.preventDefault(); moveCommandSelection(1); }
            if (event.key === "ArrowUp") { event.preventDefault(); moveCommandSelection(-1); }
            if (event.key === "Enter") {
                var selected = select("#command-results .command-result.is-selected");
                if (selected) {
                    event.preventDefault();
                    window.location.href = selected.href;
                }
            }
        });
    }

    /* ------------------------------------------------------------------ */
    /* Existing plugins: initialize once, with the original class names   */
    /* ------------------------------------------------------------------ */

    function initializeChosen(scope) {
        if (!$ || !$.fn || !$.fn.chosen) return;
        $(scope || document).find("select.chosen_multi_select, select.multi_select, select.single_select").each(function () {
            var element = $(this);
            if (element.data("chosen")) return;
            element.chosen({
                width: "100%",
                search_contains: true,
                no_results_text: "Aucun résultat pour",
                placeholder_text_multiple: "Sélectionner...",
                placeholder_text_single: "Sélectionner..."
            });
        });
    }

    function dataTableOptions() {
        return {
            lengthMenu: [[10, 25, 50, -1], [10, 25, 50, "Tous"]],
            pageLength: -1,
            autoWidth: false,
            language: {
                search: "Rechercher :",
                lengthMenu: "Afficher _MENU_ lignes",
                info: "_START_–_END_ sur _TOTAL_",
                infoEmpty: "Aucune ligne",
                zeroRecords: "Aucun résultat",
                paginate: { first: "Premier", last: "Dernier", next: "Suivant", previous: "Précédent" }
            }
        };
    }

    function initializeDataTables(scope) {
        if (!$ || !$.fn || !$.fn.DataTable) return;
        $(scope || document).find("table.filtertable").each(function () {
            if ($.fn.DataTable.isDataTable(this)) return;
            try {
                $(this).DataTable(dataTableOptions());
            } catch (error) {
                // One malformed legacy table must not prevent form launchers
                // and the rest of the page enhancements from starting.
                console.warn("Unable to enhance table", this.id || "(no id)", error);
            }
        });
    }

    /* ------------------------------------------------------------------ */
    /* Sidebar: place auto-generated links in the right section            */
    /* ------------------------------------------------------------------ */
    /* Any sidebar link WITHOUT data-nav-fixed (or inside [data-nav-auto])
       is classified from its address + label and moved into the matching
       section. Generic icons are replaced, known labels are translated,
       duplicates of curated links are removed, empty sections are dropped. */

    var NAV_GROUPS = {
        espace:       { title: "Mon espace",            icon: "fa-user-circle-o", match: /mon espace|\bespace\b/i },
        exploitation: { title: "Exploitation & TRT",    icon: "fa-tasks",         match: /exploitation|\btrt\b|gestion|t[âa]che/i },
        reception:    { title: "Réception",             icon: "fa-truck",         match: /r[ée]ception|formulaire/i },
        stocks:       { title: "Stocks & emplacements", icon: "fa-cubes",         match: /stock|emplacement/i },
        rh:           { title: "RH & paie",             icon: "fa-clock-o",       match: /pointage|paie|\brh\b|salaire|cong/i },
        config:       { title: "Configuration",         icon: "fa-cog",           match: /config|syst[èe]me|requ[êe]te/i },
        api:          { title: "API & développeur",     icon: "fa-plug",          match: /\bapi\b|d[ée]veloppeur/i },
        other:        { title: "Autres liens",          icon: "fa-link",          match: /autres|other/i }
    };

    var NAV_LINK_RULES = [
        ["api",          /openapi|swagger|redoc|\bschema\b|api-root|\/api\/|\bapi\b|\bjson\b/i],
        ["espace",       /\bmy\b|\bme\b|\bmes\b|\bmon\b|profil|my_data|mydata/i],
        ["rh",           /timesheet|pointage|attendance|salar|paie|payroll|leave|cong[ée]|absence/i],
        ["reception",    /r[ée]ception|saisie|vignette|placement|facture|commande/i],
        ["stocks",       /stock|emplacement|inventaire|magasin/i],
        ["exploitation", /schedule|\btrt\b|task|t[âa]che|planning|employee|employ|team|[ée]quipe|assign/i],
        ["config",       /quer|requ[eê]te|admin|config|setting|param|sync|import|export/i]
    ];

    var NAV_LINK_LABELS = {
        "bulk update timesheets": "Mise à jour groupée des pointages",
        "update timesheets": "Mise à jour des pointages",
        "timesheet list": "Feuilles de pointage",
        "timesheets": "Feuilles de pointage",
        "sync my data": "Synchroniser mes données",
        "bulk assign task": "Assignation groupée de tâches",
        "bulk assign tasks": "Assignation groupée de tâches",
        "reception dashboard v3": "Tableau de bord réception v3",
        "reception dashboard": "Tableau de bord réception",
        "api root": "API REST (racine)",
        "openapi json": "Schéma OpenAPI (JSON)",
        "openapi view": "Documentation API (OpenAPI)",
        "swagger ui": "Swagger UI",
        "redoc": "ReDoc",
        "schema": "Schéma OpenAPI"
    };

    function navLinkText(link) {
        var span = selectAll(":scope > span", link).filter(function (el) {
            return !el.classList.contains("nav-icon") && !el.classList.contains("nav-pill") && !el.classList.contains("nav-live") && !el.classList.contains("nav-step");
        })[0];
        return { el: span, text: (span ? span.textContent : link.textContent || "").trim() };
    }

    function navPathOf(link) {
        try { return new URL(link.getAttribute("href"), window.location.origin).pathname.replace(/\/+$/, "") || "/"; }
        catch (error) { return link.getAttribute("href") || ""; }
    }

    function classifyNavLink(link) {
        var probe = (navPathOf(link) + " " + navLinkText(link).text).toLowerCase();
        for (var r = 0; r < NAV_LINK_RULES.length; r++) {
            if (NAV_LINK_RULES[r][1].test(probe)) return NAV_LINK_RULES[r][0];
        }
        return "other";
    }

    function findNavSection(nav, group) {
        var byAttr = select('[data-nav-group="' + group + '"]', nav);
        if (byAttr) return byAttr;
        var meta = NAV_GROUPS[group];
        var sections = selectAll(".nav-section", nav).filter(function (s) { return !s.hasAttribute("data-nav-auto"); });
        for (var i = 0; i < sections.length; i++) {
            var title = select(".nav-section__title", sections[i]);
            if (title && meta && meta.match.test(title.textContent)) return sections[i];
        }
        return null;
    }

    function createNavSection(nav, group) {
        var meta = NAV_GROUPS[group] || NAV_GROUPS.other;
        var section = document.createElement("div");
        section.className = "nav-section";
        section.setAttribute("data-nav-group", group);
        var title = document.createElement("span");
        title.className = "nav-section__title";
        title.textContent = meta.title;
        section.appendChild(title);
        var anchor = select("[data-nav-auto], [data-nav-group='endpoints']", nav);
        if (anchor) nav.insertBefore(section, anchor); else nav.appendChild(section);
        return section;
    }

    function ensureNavLinkMarkup(link) {
        link.classList.add("nav-link");
        if (select(".nav-icon", link)) return;
        var text = (link.textContent || "").trim();
        link.textContent = "";
        var icon = document.createElement("span");
        icon.className = "nav-icon";
        icon.innerHTML = '<i class="fa fa-file-text-o"></i>';
        var label = document.createElement("span");
        label.textContent = text;
        link.appendChild(icon);
        link.appendChild(label);
    }

    function organizeExtraNavLinks() {
        var nav = select(".sidebar-nav");
        if (!nav) return;

        var hasFixed = !!select("a[data-nav-fixed]", nav);
        var loose = selectAll("a", nav).filter(function (a) {
            if (a.closest("[data-nav-auto]")) return true;
            if (hasFixed && !a.hasAttribute("data-nav-fixed") && !a.closest("details")) return true;
            return false;
        });
        if (!loose.length) {
            selectAll("[data-nav-auto]", nav).forEach(function (c) { if (!select("a", c)) c.remove(); });
            return;
        }

        var seen = {};
        selectAll("a", nav).forEach(function (a) { if (loose.indexOf(a) === -1) seen[navPathOf(a)] = true; });

        loose.forEach(function (link) {
            var path = navPathOf(link);
            if (seen[path]) { link.remove(); return; }
            seen[path] = true;

            ensureNavLinkMarkup(link);
            var group = classifyNavLink(link);
            var meta = NAV_GROUPS[group] || NAV_GROUPS.other;
            var section = findNavSection(nav, group) || createNavSection(nav, group);

            var icon = select(".nav-icon i", link);
            if (icon && /fa-file/.test(icon.className)) icon.className = "fa " + meta.icon;

            var info = navLinkText(link);
            var key = info.text.toLowerCase().replace(/[-_]+/g, " ").replace(/\s+/g, " ").trim();
            if (info.el && NAV_LINK_LABELS[key]) info.el.textContent = NAV_LINK_LABELS[key];

            if (group === "api") {
                link.setAttribute("target", "_blank");
                link.setAttribute("rel", "noopener noreferrer");
                if (!select(".nav-pill", link)) {
                    var pill = document.createElement("span");
                    pill.className = "nav-pill nav-pill--api";
                    pill.textContent = /json|schema/i.test(path + info.text) ? "JSON" : "API";
                    link.appendChild(pill);
                }
            }

            link.setAttribute("data-nav-auto-placed", group);
            if (link.closest(".nav-section") !== section) section.appendChild(link);
        });

        selectAll(".nav-section", nav).forEach(function (section) {
            if (!select("a, .nav-endpoint", section)) section.remove();
        });
    }

    /* ------------------------------------------------------------------ */
    /* Popup launcher for form-fragment routes (data-nav-popup)            */
    /* ------------------------------------------------------------------ */

    function ensurePopupShell() {
        var shell = select("#ounissi-popup");
        if (shell) return shell;
        shell = document.createElement("div");
        shell.id = "ounissi-popup";
        shell.className = "w3-modal";
        shell.style.display = "none";
        shell.innerHTML =
            '<div class="w3-modal-content">' +
            '  <header class="w3-container"><span class="w3-button" data-popup-close aria-label="Fermer">&times;</span><h2 data-popup-title>Formulaire</h2></header>' +
            '  <div class="w3-container" data-popup-body></div>' +
            '</div>';
        document.body.appendChild(shell);
        shell.addEventListener("click", function (event) {
            if (event.target === shell || event.target.closest("[data-popup-close]")) closePopup();
        });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && shell.style.display === "block") closePopup();
        });
        return shell;
    }

    function closePopup() {
        var shell = select("#ounissi-popup");
        if (shell) shell.style.display = "none";
    }

    UI.openPopup = function (href, title) {
        var shell = ensurePopupShell();
        select("[data-popup-title]", shell).textContent = title || "Formulaire";
        var bodyEl = select("[data-popup-body]", shell);
        bodyEl.innerHTML = '<div class="popup-loading"><div class="loading-spinner"></div></div>';
        shell.style.display = "block";

        window.fetch(href, { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } })
            .then(function (response) { return response.text(); })
            .then(function (html) {
                if ($) { $(bodyEl).html(html); } else { bodyEl.innerHTML = html; }
                initializeChosen(bodyEl);
                enhanceTables(bodyEl);
                var first = bodyEl.querySelector("input:not([type='hidden']), select, textarea");
                if (first) first.focus();
            })
            .catch(function () {
                bodyEl.innerHTML = '<div class="app-alert app-alert--error"><i class="fa fa-exclamation-circle"></i><span>Impossible de charger le formulaire.</span></div>';
            });
    };

    function initPopupLinks() {
        selectAll("a[data-nav-popup]").forEach(function (link) {
            link.addEventListener("click", function (event) {
                if (event.ctrlKey || event.metaKey || event.shiftKey) return;   // let the user open it raw if they insist
                event.preventDefault();
                UI.openPopup(link.href, link.getAttribute("data-nav-popup") || navLinkText(link).text);
            });
        });
    }

    /* ------------------------------------------------------------------ */
    /* Non-destructive content enhancement                                */
    /* ------------------------------------------------------------------ */

    function enhanceTables(scope) {
        selectAll("table", scope || document).forEach(function (table) {
            if (table.dataset.uiEnhanced === "true") return;

            // Never touch tables that are already managed by a plugin or that
            // live inside a modal — create_schedule.js and the reception forms
            // build rows dynamically and rely on the original DOM position.
            if (
                table.closest(".dataTables_wrapper") ||
                table.closest(".table-scroll-shell") ||
                table.closest(".modal") ||
                table.closest(".w3-modal") ||
                table.hasAttribute("data-no-shell") ||
                table.classList.contains("ui-plain-table")
            ) {
                table.dataset.uiEnhanced = "true";
                return;
            }

            // Only add a scroll container when the table really is wider than
            // its parent. Matrix tables (stock / emplacements) must keep their
            // natural layout and must NOT be forced into a horizontal scroller.
            var parent = table.parentElement;
            var needsShell = false;
            if (parent) {
                var available = parent.clientWidth || document.documentElement.clientWidth;
                if (table.scrollWidth > available + 2) needsShell = true;
            }

            if (needsShell) {
                var shell = document.createElement("div");
                shell.className = "table-scroll-shell";
                table.parentNode.insertBefore(shell, table);
                shell.appendChild(table);
            }
            table.dataset.uiEnhanced = "true";
        });
    }

    /* Re-check table overflow after fonts/layout settle and on resize. */
    function reflowTables() {
        selectAll(".table-scroll-shell > table").forEach(function (table) {
            var shell = table.parentElement;
            if (!shell) return;
            if (table.scrollWidth <= shell.clientWidth + 2) {
                shell.replaceWith(table);
                table.dataset.uiEnhanced = "processed";
            }
        });
        enhanceTables(document);
    }

    function enhanceProgressBars(scope) {
        selectAll(".w3-grey", scope || document).forEach(function (track) {
            if (track.dataset.uiEnhanced === "true") return;
            var bar = select(".w3-green, [style*='width']", track);
            if (!bar) return;
            var text = (bar.textContent || "").trim();
            if (!text) {
                var width = bar.style.width;
                text = width || "";
            }
            if (text) {
                var label = document.createElement("span");
                label.className = "progress-value-label";
                label.textContent = text.indexOf("%") === -1 ? text + "%" : text;
                track.insertAdjacentElement("afterend", label);
            }
            track.dataset.uiEnhanced = "true";
        });
    }

    function colorEmptyCells(scope) {
        selectAll("td", scope || document).forEach(function (cell) {
            var value = (cell.textContent || "").trim();
            if (value === "None" || value === "null" || value === "N/A") {
                cell.classList.add("ui-empty-value");
                cell.style.color = "var(--ui-rose)";
            }
        });
    }

    function enhanceExternalLinks(scope) {
        selectAll("a[target='_blank']", scope || document).forEach(function (link) {
            var rel = (link.getAttribute("rel") || "").split(/\s+/);
            if (rel.indexOf("noopener") === -1) rel.push("noopener");
            if (rel.indexOf("noreferrer") === -1) rel.push("noreferrer");
            link.setAttribute("rel", rel.join(" ").trim());
        });
    }

    function observeDynamicContent() {
        if (!("MutationObserver" in window)) return;
        var content = select("[data-page-content]");
        if (!content) return;

        var queued = false;
        var observer = new MutationObserver(function (mutations) {
            if (queued) return;
            var containsElements = mutations.some(function (mutation) { return mutation.addedNodes && mutation.addedNodes.length; });
            if (!containsElements) return;
            queued = true;
            window.requestAnimationFrame(function () {
                queued = false;
                initializeChosen(content);
                initializeDataTables(content);
                enhanceTables(content);
                enhanceProgressBars(content);
                colorEmptyCells(content);
            });
        });
        observer.observe(content, { childList: true, subtree: true });
    }

    /* ------------------------------------------------------------------ */
    /* Request feedback; fetch/jQuery behavior and responses stay intact   */
    /* ------------------------------------------------------------------ */

    var requestCount = 0;
    function requestStart() {
        requestCount += 1;
        var progress = select("#app-progress");
        if (progress) {
            progress.classList.remove("is-complete");
            progress.classList.add("is-loading");
        }
    }

    function requestEnd() {
        requestCount = Math.max(0, requestCount - 1);
        if (requestCount) return;
        var progress = select("#app-progress");
        if (!progress) return;
        progress.classList.add("is-complete");
        window.setTimeout(function () {
            progress.classList.remove("is-loading", "is-complete");
        }, 260);
    }

    function initRequestFeedback() {
        if (window.fetch && !window.fetch.__ounissiWrapped) {
            var originalFetch = window.fetch;
            var wrappedFetch = function () {
                requestStart();
                return originalFetch.apply(this, arguments).then(function (response) {
                    requestEnd();
                    return response;
                }, function (error) {
                    requestEnd();
                    throw error;
                });
            };
            wrappedFetch.__ounissiWrapped = true;
            window.fetch = wrappedFetch;
        }

        if ($) {
            $(document).on("ajaxSend.ounissi", requestStart);
            // ajaxComplete fires for both success and failure; also counting
            // ajaxError would finish the same request twice.
            $(document).on("ajaxComplete.ounissi", requestEnd);
        }

        selectAll("form").forEach(function (form) {
            form.addEventListener("submit", function (event) {
                if (form.target === "_blank") return;
                if (event.submitter && event.submitter.name === "export") return;
                if (typeof form.checkValidity === "function" && !form.checkValidity()) return;
                // Other handlers may cancel submission to perform an AJAX request.
                // Check after all listeners ran so the progress bar cannot stick.
                window.setTimeout(function () {
                    if (!event.defaultPrevented) requestStart();
                }, 0);
            });
        });

        selectAll("a[href]").forEach(function (link) {
            link.addEventListener("click", function (event) {
                if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
                if (link.target === "_blank" || link.hasAttribute("download")) return;
                var href = link.getAttribute("href");
                if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return;
                try {
                    var url = new URL(link.href, window.location.origin);
                    if (url.origin === window.location.origin) requestStart();
                } catch (error) { /* malformed URL */ }
            });
        });

        window.addEventListener("pageshow", function () {
            requestCount = 0;
            requestEnd();
            window.hideLoadingContainer();
        });
    }

    /* ------------------------------------------------------------------ */
    /* Alerts / activity                                                  */
    /* ------------------------------------------------------------------ */

    UI.toast = function (message, iconClass, timeout) {
        var region = select("#app-toast-region");
        if (!region) return;
        var toast = document.createElement("div");
        toast.className = "app-toast";
        var icon = document.createElement("i");
        icon.className = iconClass || "fa fa-check-circle";
        var copy = document.createElement("span");
        copy.textContent = message;
        toast.appendChild(icon);
        toast.appendChild(copy);
        region.appendChild(toast);
        window.setTimeout(function () {
            toast.classList.add("is-leaving");
            window.setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 190);
        }, timeout || 3200);
    };

    function initAlerts() {
        selectAll("[data-alert-close]").forEach(function (button) {
            button.addEventListener("click", function () {
                var alert = button.closest(".app-alert");
                if (alert) alert.remove();
            });
        });

        selectAll("a[data-unwired-route]").forEach(function (link) {
            link.addEventListener("click", function (event) {
                var routeName = link.getAttribute("data-unwired-route");
                var tplPath = link.getAttribute("data-template-path") || "";
                var targetHref = link.getAttribute("href");
                UI.toast(
                    "Route Django '" + routeName + "' (" + tplPath + ") — vérifiez qu'elle est active dans urls.py.",
                    "fa fa-info-circle",
                    4800
                );
                if (!targetHref || targetHref === "#") {
                    event.preventDefault();
                }
            });
        });
    }

    /* ------------------------------------------------------------------ */
    /* Bootstrap modal focus repair for dynamically injected form content  */
    /* ------------------------------------------------------------------ */

    function initModalCompatibility() {
        if (!$) return;
        $(document).on("shown.bs.modal", ".modal", function () {
            initializeChosen(this);
            enhanceTables(this);
            var first = this.querySelector("input:not([type='hidden']), select, textarea, button:not([data-dismiss])");
            if (first) first.focus();
        });

        $(document).on("hidden.bs.modal", ".modal", function () {
            $(".modal-backdrop").remove();
            if (!$(".modal.show").length) $(document.body).removeClass("modal-open").css("padding-right", "");
        });
    }

    /* ------------------------------------------------------------------ */
    /* Boot                                                               */
    /* ------------------------------------------------------------------ */

    ready(function () {
        body = document.body;
        initTheme();
        initSidebar();
        organizeExtraNavLinks();      // must run before active-link detection & the palette index
        initActiveNavigation();
        derivePageIdentity();
        updateClocks();
        window.setInterval(updateClocks, 1000);

        initCommandPalette();
        initPopupLinks();             // before initRequestFeedback so preventDefault is honoured
        initializeChosen(document);
        initializeDataTables(document);
        enhanceTables(document);
        enhanceProgressBars(document);
        colorEmptyCells(document);
        enhanceExternalLinks(document);
        observeDynamicContent();

        initRequestFeedback();
        initAlerts();
        initModalCompatibility();
        window.hideLoadingContainer();

        /* Tables: re-measure once everything (fonts, Chosen, DataTables) has
           settled, then on resize. This is what keeps matrix tables such as
           /stock/emplacements/ in their natural vertical layout. */
        window.addEventListener("load", reflowTables);
        window.addEventListener("resize", debounce(reflowTables, 180));

        /* Broadcast after the UI layer is ready; existing code may opt in. */
        document.dispatchEvent(new CustomEvent("ounissi:ui-ready", { detail: { version: "2.0" } }));
    });
})(window, document, window.jQuery);