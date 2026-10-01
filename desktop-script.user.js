// ==UserScript==
// @name         Mangatan - Better Text Boxes & Mining - Refractored
// @namespace    http://tampermonkey.net/
// @version      24.6.14
// @description  Adds a stable, inline OCR button and modifier-key merging. Now includes a superior CSS blend mode for perfect text contrast on any background. This version includes significant stability improvements to the hover-to-show overlay logic, eliminating flickering. Includes fixes for font size calculation, merged box containment, widow/orphan prevention, and resilience against OCR errors causing text overflow. Now with editable OCR text boxes. Fixed image export bug where wrong chapter images were being captured. Includes duplicate punctuation removal. Fixed merge selection reset on mouse leave. Added multi-image selector for dual-page layouts. NEW: FAB Menu with Toggleable Edit/Merge modes and Pickaxe Anki Icon. Menu now auto-collapses on selection. NEW: Smart Hybrid Input system restores Yomitan & Native Scroll/Swipe perfectly. Performance improvements included.
// @author       1Selxo (Original) & Gemini (Refactoring & PC-Centric Features) & Modified for OCR Error Resilience & Editable Text & Image Export Fix & Punctuation Fix & Merge Stability & Multi-Image Selector & FAB Menu & Native Scroll Fix
// @match        *://127.0.0.1:4567/*
// @match        *://localhost:4567/*
// @match        *://suwayomi*/*
// @exclude      *://suwayomi.org/*
// @exclude      *://github.com/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_addStyle
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @downloadURL  https://github.com/exn251/Mangatan/raw/refs/heads/main/desktop-script.user.js
// @updateURL    https://github.com/exn251/Mangatan/raw/refs/heads/main/desktop-script.user.js
// ==/UserScript==

(function () {
    'use strict';

    // --- Global State and Settings ---
    let isOcrPaused = false;
    let settings = {
        ocrServerUrl: 'http://127.0.0.1:3000',
        imageServerUser: '',
        imageServerPassword: '',
        ankiConnectUrl: 'http://127.0.0.1:8765',
        ankiImageField: 'Picture',
        sites:[{
            urlPattern: '127.0.0.1',
            imageContainerSelectors:[
                'img[src*="/api/v1/manga/"][src*="/page/"]',
                'div:has(> img[src*="/api/v1/manga/"])',
                'img[draggable="false"][crossorigin="anonymous"]'
            ],
            overflowFixSelector: '#root div:has(> div > div > img[src*="/api/v1/manga/"])',
            contentRootSelector: '#root'
        }],
        debugMode: false, textOrientation: 'smart', interactionMode: 'hover', dimmedOpacity: 0.3,
        fontMultiplierHorizontal: 1.0, fontMultiplierVertical: 1.0, boundingBoxAdjustment: 5,
        focusScaleMultiplier: 1.1, soloHoverMode: true, deleteModifierKey: 'Alt',
        mergeModifierKey: 'Control', addSpaceOnMerge: false, colorTheme: 'white',
        brightnessMode: 'light', focusFontColor: 'black',
        mobileMode: true,
        mobileEditMerging: false
    };
    let debugLog =[];
    const SETTINGS_KEY = 'gemini_ocr_settings_v24_pc_focus_color_ocr_error_resilience_editable';
    const ocrDataCache = new WeakMap();
    const managedElements = new Map(), managedContainers = new Map(), attachedAttributeObservers = new WeakMap();
    let activeSiteConfig = null, measurementSpan = null, activeImageForExport = null, activeOverlay = null;
    const UI = {};
    let mergeState = { anchorBox: null };

    // Tool toggle states
    const toolState = {
        isMergeMode: false,
        isEditMode: false,
        isDeleteMode: false,
        isMenuOpen: false
    };

    let resizeObserver, intersectionObserver, imageObserver, containerObserver, chapterObserver, navigationObserver;
    const visibleImages = new Set();
    let animationFrameId = null;

    const recentlyHoveredImages = new Set();
    const MAX_RECENT_IMAGES = 3;

    const COLOR_THEMES = {
        blue: { accent: '72,144,255', background: '229,243,255' }, red: { accent: '255,72,75', background: '255,229,230' },
        green: { accent: '34,119,49', background: '239,255,229' }, orange: { accent: '243,156,18', background: '255,245,229' },
        purple: { accent: '155,89,182', background: '245,229,255' }, turquoise: { accent: '26,188,156', background: '229,255,250' },
        pink: { accent: '255,77,222', background: '255,229,255' }, grey: { accent: '149,165,166', background: '229,236,236' },
        white: { accent: '70,70,70', background: '255,255,255' }
    };

    // Editable Text Box State
    const editableState = {
        activeEditBox: null,
        originalText: null,
        originalStyles: null
    };

    const logDebug = (message) => {
        if (!settings.debugMode) return;
        const timestamp = new Date().toLocaleTimeString(), logEntry = `[${timestamp}] ${message}`;
        console.log(`[OCR PC Hybrid] ${logEntry}`);
        debugLog.push(logEntry);
        document.dispatchEvent(new CustomEvent('ocr-log-update'));
    };

    // --- OCR Duplicate Punctuation Remover Functions ---
    function cleanPunctuation(text) {
        if (!text) return text;
        return text
            .replace(/[ ]*!!+/g, '‼')
            .replace(/[ ]*\?\?+/g, '⁇')
            .replace(/[ ]*\.\.+/g, '\u2026')
            .replace(/[ ]*(!\?)+/g, '⁉')
            .replace(/[ ]*(\?!)+/g, '⁈')
            .replace(/[ ]*\u2026+/g, '\u2026')
            .replace(/[ ]*[\u30FB\uFF65]{2,}/g, '\u2026')
            .replace(/[ ]*\u00B7{2,}/g, '\u2026')
            .replace(/[ ]*-+/g, '\u30FC')
            .replace(/[ ]*\u2013+/g, '\u2015')
            .replace(/[ ]*:+[ ]*/g, '\u2026')
            .replace(/^[!?:]+$/g, '')
            .replace(/([⁉⁈‼⁇])[!?:]+/g, '$1')
            .replace(/[!?:]+([⁉⁈‼⁇])/g, '$1')
            .replace(/\u0020/g, '');
    }

    // --- Navigation Handling & State Reset ---
    function fullCleanupAndReset() {
        logDebug("NAVIGATION DETECTED: Starting full cleanup and reset.");
        if (animationFrameId !== null) { cancelAnimationFrame(animationFrameId); animationFrameId = null; }
        if (containerObserver) containerObserver.disconnect(); if (imageObserver) imageObserver.disconnect(); if (chapterObserver) chapterObserver.disconnect();
        for (const [img, state] of managedElements.entries()) {
            if (state.overlay?.isConnected) state.overlay.remove();
            if (state.hideTimer) clearTimeout(state.hideTimer);
            resizeObserver.unobserve(img); intersectionObserver.unobserve(img);
        }
        managedElements.clear(); managedContainers.clear(); visibleImages.clear(); hideActiveOverlay();

        activeImageForExport = null;
        recentlyHoveredImages.clear();

        editableState.activeEditBox = null;
        editableState.originalText = null;
        editableState.originalStyles = null;

        mergeState.anchorBox = null;
        logDebug("All state maps cleared. Cleanup complete.");
    }

    function cleanupDisconnectedImages() {
        let cleaned = 0;
        for (const[img, state] of managedElements.entries()) {
            if (!img.isConnected) {
                if (state.overlay?.isConnected) state.overlay.remove();
                if (state.hideTimer) clearTimeout(state.hideTimer);
                resizeObserver.unobserve(img);
                intersectionObserver.unobserve(img);
                managedElements.delete(img);
                visibleImages.delete(img);
                cleaned++;
            }
        }
        if (cleaned > 0) {
            logDebug(`Cleaned up ${cleaned} disconnected images from managedElements`);
        }
    }

    setInterval(cleanupDisconnectedImages, 10000);

    function reinitializeScript() { logDebug("Re-initializing scanners."); activateScanner(); observeChapters(); }
    function setupNavigationObserver() {
        const contentRootSelector = activeSiteConfig?.contentRootSelector;
        if (!contentRootSelector) return logDebug("Warning: No `contentRootSelector` defined.");
        const targetNode = document.querySelector(contentRootSelector);
        if (!targetNode) return logDebug(`Navigation observer target not found: ${contentRootSelector}.`);
        navigationObserver = new MutationObserver((mutations) => {
            for (const mutation of mutations) for (const node of mutation.removedNodes)
                if (node.nodeType === 1 && (managedContainers.has(node) || managedElements.has(node))) {
                    fullCleanupAndReset();
                    setTimeout(reinitializeScript, 250);
                    return;
                }
        });
        navigationObserver.observe(targetNode, { childList: true, subtree: true });
        logDebug(`Robust navigation observer attached to ${targetNode.id || targetNode.className}.`);
    }

    function setupPageChangeDetection() {
        let lastVisibleImageSrcs = new Set();

        setInterval(() => {
            const currentVisibleImageSrcs = new Set();

            for (const img of visibleImages) {
                if (img.isConnected && img.src) {
                    currentVisibleImageSrcs.add(img.src);
                }
            }

            if (lastVisibleImageSrcs.size > 0 && currentVisibleImageSrcs.size > 0) {
                const hasOverlap = [...currentVisibleImageSrcs].some(src =>
                    lastVisibleImageSrcs.has(src)
                );

                if (!hasOverlap) {
                    logDebug("Page turn detected - clearing recent images");
                    recentlyHoveredImages.clear();
                    activeImageForExport = null;
                }
            }

            lastVisibleImageSrcs = currentVisibleImageSrcs;
        }, 2000);
    }

    // --- Hybrid Render Engine Core ---
    function updateVisibleOverlaysPosition() {
        for (const img of visibleImages) {
            const state = managedElements.get(img);
            if (state?.overlay.isConnected) {
                const rect = img.getBoundingClientRect();

                if (Math.abs((state.lastTop || 0) - rect.top) > 0.5 ||
                    Math.abs((state.lastLeft || 0) - rect.left) > 0.5) {

                    state.overlay.style.top = `${rect.top}px`;
                    state.overlay.style.left = `${rect.left}px`;
                    state.lastTop = rect.top;
                    state.lastLeft = rect.left;
                }
            }
        }
        animationFrameId = requestAnimationFrame(updateVisibleOverlaysPosition);
    }

    function updateOverlayDimensionsAndStyles(img, state, rect = null) {
        if (!rect) rect = img.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
            Object.assign(state.overlay.style, { width: `${rect.width}px`, height: `${rect.height}px` });
            if (state.lastWidth !== rect.width || state.lastHeight !== rect.height) {
                if (state.overlay.classList.contains('is-focused')) {
                    calculateAndApplyOptimalStyles_Optimized(state.overlay, rect);
                }
                state.lastWidth = rect.width; state.lastHeight = rect.height;
            }
        }
    }
    const handleResize = (entries) => {
        for (const entry of entries) if (managedElements.has(entry.target))
            updateOverlayDimensionsAndStyles(entry.target, managedElements.get(entry.target), entry.contentRect);
    };
    const handleIntersection = (entries) => {
        for (const entry of entries) {
            const img = entry.target;
            if (entry.isIntersecting) {
                if (!visibleImages.has(img)) {
                    visibleImages.add(img);
                    const state = managedElements.get(img);
                    if (state) state.overlay.style.visibility = 'visible';
                    if (animationFrameId === null) animationFrameId = requestAnimationFrame(updateVisibleOverlaysPosition);
                }
            } else if (visibleImages.has(img)) {
                visibleImages.delete(img);
                const state = managedElements.get(img);
                if (state) state.overlay.style.visibility = 'hidden';
                if (visibleImages.size === 0 && animationFrameId !== null) {
                    cancelAnimationFrame(animationFrameId); animationFrameId = null;
                }
            }
        }
    };

    // --- Core Observation Logic ---
    function setupMutationObservers() {
        imageObserver = new MutationObserver((mutations) => {
            for (const m of mutations) for (const n of m.addedNodes) if (n.nodeType === 1) {
                if (n.tagName === 'IMG') observeImageForSrcChange(n);
                else n.querySelectorAll('img').forEach(observeImageForSrcChange);
            }
        });
        containerObserver = new MutationObserver((mutations) => {
            if (!activeSiteConfig) return;
            const sel = activeSiteConfig.imageContainerSelectors.join(', ');
            for (const m of mutations) for (const n of m.addedNodes) if (n.nodeType === 1) {
                if (n.matches(sel)) manageContainer(n);
                else n.querySelectorAll(sel).forEach(manageContainer);
            }
        });
        chapterObserver = new MutationObserver((mutations) => {
            for (const m of mutations) for (const n of m.addedNodes) if (n.nodeType === 1) {
                const links = n.matches('a[href*="/manga/"][href*="/chapter/"]') ? [n] : n.querySelectorAll('a[href*="/manga/"][href*="/chapter/"]');
                links.forEach(addOcrButtonToChapter);
            }
        });
    }
    function manageContainer(container) {
        if (!container || managedContainers.has(container)) return;
        logDebug(`New container found: ${container.className || container.tagName}`);

        if (container.tagName === 'IMG') {
            observeImageForSrcChange(container);
        } else {
            container.querySelectorAll('img').forEach(observeImageForSrcChange);
            imageObserver.observe(container, { childList: true, subtree: true });
        }
        managedContainers.set(container, true);
    }
    function activateScanner() {
        activeSiteConfig = settings.sites.find(site => window.location.href.includes(site.urlPattern));
        if (!activeSiteConfig?.imageContainerSelectors?.length) return logDebug(`No matching site config for URL: ${window.location.href}.`);
        const sel = activeSiteConfig.imageContainerSelectors.join(', ');
        document.querySelectorAll(sel).forEach(manageContainer);
        containerObserver.observe(document.body, { childList: true, subtree: true });
        logDebug("Main container observer is active.");
    }
    function observeChapters() {
        const targetNode = document.getElementById('root');
        if (!targetNode) return;
        targetNode.querySelectorAll('a[href*="/manga/"][href*="/chapter/"]').forEach(addOcrButtonToChapter);
        chapterObserver.observe(targetNode, { childList: true, subtree: true });
    }

    // --- Image Handling & OCR ---
    function observeImageForSrcChange(img) {
        if (img.hasAttribute('alt')) {
            img.removeAttribute('alt');
        }
        const process = (src) => {
            // Only process reader pages; ignore library thumbnails
            if (src && src.includes('/api/v1/manga/') && src.includes('/page/') && !src.includes('/thumbnail')) {
                primeImageForOcr(img);
                return true;
            }
            return false;
        };
        if (process(img.src) || attachedAttributeObservers.has(img)) return;
        const attrObserver = new MutationObserver((mutations) => {
            if (mutations.some(m => m.attributeName === 'src' && process(img.src))) {
                attrObserver.disconnect(); attachedAttributeObservers.delete(img);
            }
        });
        attrObserver.observe(img, { attributes: true });
        attachedAttributeObservers.set(img, attrObserver);
    }

    function primeImageForOcr(img) {
        if (managedElements.has(img) || ocrDataCache.get(img) === 'pending') return;
        const doProcess = () => { img.crossOrigin = "anonymous"; processImage(img, img.src); };
        if (img.complete && img.naturalHeight > 0) doProcess(); else img.addEventListener('load', doProcess, { once: true });
    }

    function processImage(img, sourceUrl) {
        if (ocrDataCache.has(img)) { displayOcrResults(img); return; }

        const normalizedUrl = sourceUrl.split('?')[0];

        logDebug(`Requesting OCR for ...${normalizedUrl.slice(-30)}`);
        ocrDataCache.set(img, 'pending');
        const context = document.title;
        let ocrRequestUrl = `${settings.ocrServerUrl}/ocr?url=${encodeURIComponent(normalizedUrl)}&context=${encodeURIComponent(context)}`;
        if (settings.imageServerUser) ocrRequestUrl += `&user=${encodeURIComponent(settings.imageServerUser)}&pass=${encodeURIComponent(settings.imageServerPassword)}`;
        GM_xmlhttpRequest({
            method: 'GET', url: ocrRequestUrl, timeout: 45000,
            onload: (res) => {
                try {
                    const data = JSON.parse(res.responseText);
                    if (data.error) throw new Error(data.error);
                    if (!Array.isArray(data)) throw new Error('Server response not a valid OCR data array.');

                    const cleanedData = data.map(item => ({
                        ...item,
                        text: cleanPunctuation(item.text)
                    }));

                    ocrDataCache.set(img, cleanedData);
                    displayOcrResults(img);
                } catch (e) {
                    logDebug(`OCR Error for ${sourceUrl.slice(-30)}: ${e.message}`);
                    ocrDataCache.delete(img);
                }
            },
            onerror: () => { logDebug(`Connection error.`); ocrDataCache.delete(img); },
            ontimeout: () => { logDebug(`Request timed out.`); ocrDataCache.delete(img); }
        });
    }

    // --- Rendering & Interaction Logic ---
    function calculateAndApplyStylesForSingleBox(box, imgRect) {
        if (editableState.activeEditBox === box) return;

        if (!measurementSpan || !box || !imgRect || imgRect.width === 0 || imgRect.height === 0) return;
        const ocrData = box._ocrData, text = ocrData.text || '';

        box.style.width = `${ocrData.tightBoundingBox.width * 100}%`;
        box.style.height = `${ocrData.tightBoundingBox.height * 100}%`;
        box.style.left = `${ocrData.tightBoundingBox.x * 100}%`;
        box.style.top = `${ocrData.tightBoundingBox.y * 100}%`;

        const availableWidth = (box.offsetWidth + settings.boundingBoxAdjustment) * 1.00;
        const availableHeight = (box.offsetHeight + settings.boundingBoxAdjustment) * 1.00;

        if (!text || availableWidth <= 0 || availableHeight <= 0) return;

        const MINIMUM_FONT_SIZE = 24;
        const isMerged = ocrData.isMerged || text.includes('\u200B');

        const findBestFitSizeMath = (isVerticalSearch) => {
            measurementSpan.style.writingMode = isVerticalSearch ? 'vertical-rl' : 'horizontal-tb';
            measurementSpan.style.whiteSpace = isMerged ? 'pre' : 'normal';

            if (isMerged) {
                measurementSpan.innerHTML = text.replace(/\u200B/g, "<br>");
            } else {
                measurementSpan.textContent = text;
            }

            const baseFontSize = 50;
            measurementSpan.style.fontSize = `${baseFontSize}px`;

            const mw = measurementSpan.offsetWidth;
            const mh = measurementSpan.offsetHeight;

            if (mw === 0 || mh === 0) return 1;

            const scaleW = availableWidth / mw;
            const scaleH = availableHeight / mh;
            const scale = Math.min(scaleW, scaleH);

            return Math.max(1, Math.min(Math.floor(baseFontSize * scale), 200));
        };

        const horizontalFitSize = findBestFitSizeMath(false);
        const verticalFitSize   = findBestFitSizeMath(true);

        let finalFontSize = 0, isVertical = false;
        if (ocrData.forcedOrientation === 'vertical') {
            isVertical = true;
            finalFontSize = verticalFitSize;
        } else if (ocrData.forcedOrientation === 'horizontal') {
            isVertical = false;
            finalFontSize = horizontalFitSize;
        } else if (settings.textOrientation === 'forceVertical') {
            isVertical = true;
            finalFontSize = verticalFitSize;
        } else if (settings.textOrientation === 'forceHorizontal') {
            isVertical = false;
            finalFontSize = horizontalFitSize;
        } else {
            isVertical = verticalFitSize > horizontalFitSize;
            finalFontSize = isVertical ? verticalFitSize : horizontalFitSize;
        }

        const multiplier = isVertical ? settings.fontMultiplierVertical : settings.fontMultiplierHorizontal;
        const requiredFontSize = Math.max(finalFontSize, MINIMUM_FONT_SIZE);

        if (requiredFontSize > finalFontSize) {
            measurementSpan.style.fontSize = `${requiredFontSize * multiplier}px`;
            measurementSpan.style.writingMode = isVertical ? 'vertical-rl' : 'horizontal-tb';

            const scaledRequiredWidth = measurementSpan.offsetWidth * 1.1;
            const scaledRequiredHeight = measurementSpan.offsetHeight * 1.1;

            if (imgRect.width > 0 && imgRect.height > 0) {
                const currentWidthPercent = parseFloat(box.style.width);
                const currentHeightPercent = parseFloat(box.style.height);

                const requiredWidthPercent = (scaledRequiredWidth / imgRect.width) * 100;
                const requiredHeightPercent = (scaledRequiredHeight / imgRect.height) * 100;

                if (requiredWidthPercent > currentWidthPercent * 1.2 ||
                    requiredHeightPercent > currentHeightPercent * 1.2) {

                    const newWidth = Math.max(currentWidthPercent, requiredWidthPercent);
                    const newHeight = Math.max(currentHeightPercent, requiredHeightPercent);
                    const widthAdjustment = (newWidth - currentWidthPercent) / 2;
                    const heightAdjustment = (newHeight - currentHeightPercent) / 2;

                    box.style.width = `${newWidth}%`;
                    box.style.height = `${newHeight}%`;
                    box.style.left = `${parseFloat(box.style.left) - widthAdjustment}%`;
                    box.style.top = `${parseFloat(box.style.top) - heightAdjustment}%`;
                }
            }
        }

        box.style.fontSize = `${requiredFontSize * multiplier}px`;
        box.classList.toggle('gemini-ocr-text-vertical', isVertical);

        if (isMerged) {
            box.style.whiteSpace = 'pre';
            box.style.textAlign = 'start';
            box.innerHTML = text.replace(/\u200B/g, "<br>");
        } else {
            box.style.whiteSpace = 'nowrap';
            box.style.textAlign = 'center';
            box.textContent = text;
        }
    }

    function calculateAndApplyOptimalStyles_Optimized(overlay, imgRect) {
        if (!measurementSpan || imgRect.width === 0 || imgRect.height === 0) return;

        const dimensionKey = `${Math.round(imgRect.width)}x${Math.round(imgRect.height)}`;
        if (overlay._lastCalcDimensions === dimensionKey && !overlay._forceRecalc) {
            return;
        }

        const boxes = Array.from(overlay.querySelectorAll('.gemini-ocr-text-box'));
        if (boxes.length === 0) return;

        const baseStyle = getComputedStyle(boxes[0]);
        Object.assign(measurementSpan.style, {
            fontFamily: baseStyle.fontFamily,
            fontWeight: baseStyle.fontWeight,
            letterSpacing: baseStyle.letterSpacing
        });

        for (const box of boxes) {
            calculateAndApplyStylesForSingleBox(box, imgRect);
        }

        measurementSpan.style.writingMode = 'horizontal-tb';

        overlay._lastCalcDimensions = dimensionKey;
        overlay._forceRecalc = false;
    }

    function showOverlay(overlay, image) {
        if (isOcrPaused) return; // Do not show overlay when paused

        if (activeOverlay && activeOverlay !== overlay) hideActiveOverlay();
        activeOverlay = overlay;

        const currentChapterMatch = window.location.pathname.match(/\/manga\/\d+\/chapter\/\d+/);
        const imageChapterMatch   = image.src.match(/\/manga\/\d+\/chapter\/\d+/);

        if (currentChapterMatch && imageChapterMatch && currentChapterMatch[0] === imageChapterMatch[0]) {
            activeImageForExport = image;

            recentlyHoveredImages.add(image);
            if (recentlyHoveredImages.size > MAX_RECENT_IMAGES) {
                const firstImage = recentlyHoveredImages.values().next().value;
                recentlyHoveredImages.delete(firstImage);
            }

            logDebug(`Active image set to: ${image.src.slice(-50)}`);
        } else {
            logDebug(`Skipping image from different chapter: ${image.src.slice(-50)}`);
        }

        overlay.classList.add('is-focused');

        if (mergeState.anchorBox && overlay.contains(mergeState.anchorBox)) {
            mergeState.anchorBox.classList.add('selected-for-merge');
        }

        const rect = image.getBoundingClientRect();
        calculateAndApplyOptimalStyles_Optimized(overlay, rect);
    }

    function hideActiveOverlay() {
        if (!activeOverlay) return;
        const overlayToHide = activeOverlay;
        overlayToHide.classList.remove('is-focused', 'has-manual-highlight');
        overlayToHide.querySelectorAll('.manual-highlight').forEach(b => b.classList.remove('manual-highlight'));
        activeOverlay = null;
    }

    function isModifierPressed(event, keyName) {
        if (!keyName) return false;
        const k = keyName.toLowerCase();
        return (k === 'ctrl' || k === 'control') ? event.ctrlKey
            : (k === 'alt') ? event.altKey
            : (k === 'shift') ? event.shiftKey
            : (k === 'meta' || k === 'win' || k === 'cmd') ? event.metaKey
            : false;
    }

    function handleBoxDelete(boxElement, sourceImage) {
        logDebug(`Deleting box: "${boxElement.dataset.fullText}"`);
        const data = ocrDataCache.get(sourceImage);
        if (!data) return;
        const updatedData = data.filter((item, index) => index !== boxElement._ocrDataIndex);
        ocrDataCache.set(sourceImage, updatedData);
        boxElement.remove();

        if (mergeState.anchorBox === boxElement) {
            mergeState.anchorBox = null;
        }

        syncCacheToServer(sourceImage).then(success => {
            if (!success) {
                logDebug("Warning: Failed to sync deletion to server cache");
            }
        });
    }

    function handleBoxMerge(targetBox, sourceBox, sourceImage, overlay) {
        const targetText = targetBox.dataset.fullText || targetBox.textContent;
        const sourceText = sourceBox.dataset.fullText || sourceBox.textContent;

        logDebug(`Merging "${targetText}" with "${sourceText}"`);
        const originalData = ocrDataCache.get(sourceImage);
        if (!originalData) return;
        const targetData = targetBox._ocrData;
        const sourceData = sourceBox._ocrData;

        let combinedText = targetText + (settings.addSpaceOnMerge ? ' ' : "\u200B") + sourceText;

        const tb = targetData.tightBoundingBox;
        const sb = sourceData.tightBoundingBox;
        const newRight  = Math.max(tb.x + tb.width,  sb.x + sb.width);
        const newBottom = Math.max(tb.y + tb.height, sb.y + sb.height);
        const newBoundingBox = {
            x: Math.min(tb.x, sb.x),
            y: Math.min(tb.y, sb.y),
            width: 0,
            height: 0
        };
        newBoundingBox.width  = newRight  - newBoundingBox.x;
        newBoundingBox.height = newBottom - newBoundingBox.y;
        const areBothVertical = targetBox.classList.contains('gemini-ocr-text-vertical') && sourceBox.classList.contains('gemini-ocr-text-vertical');
        const newOcrItem = {
            text: combinedText,
            tightBoundingBox: newBoundingBox,
            forcedOrientation: areBothVertical ? 'vertical' : 'auto',
            isMerged: true
        };
        const indicesToDelete = new Set([targetBox._ocrDataIndex, sourceBox._ocrDataIndex]);
        const newData = originalData.filter((item, index) => !indicesToDelete.has(index));
        newData.push(newOcrItem);
        ocrDataCache.set(sourceImage, newData);
        targetBox.remove();
        sourceBox.remove();

        const newBoxElement = document.createElement('div');
        newBoxElement.className = 'gemini-ocr-text-box';

        const displayText = combinedText.replace(/\u200B/g, "\n");
        newBoxElement.textContent = displayText;
        newBoxElement.dataset.fullText = combinedText;
        newBoxElement.dataset.originalText = combinedText;
        newBoxElement._ocrData = newOcrItem;
        newBoxElement._ocrDataIndex = newData.length - 1;

        newBoxElement.style.whiteSpace = 'pre';
        newBoxElement.style.textAlign = 'start';

        Object.assign(newBoxElement.style, {
            left:   `${newOcrItem.tightBoundingBox.x * 100}%`,
            top:    `${newOcrItem.tightBoundingBox.y * 100}%`,
            width:  `${newOcrItem.tightBoundingBox.width * 100}%`,
            height: `${newOcrItem.tightBoundingBox.height * 100}%`
        });
        overlay.appendChild(newBoxElement);
        overlay._forceRecalc = true;
        calculateAndApplyStylesForSingleBox(newBoxElement, sourceImage.getBoundingClientRect());

        mergeState.anchorBox = null;
        overlay.classList.remove('merging');

        document.querySelectorAll('.gemini-ocr-text-box.selected-for-merge')
            .forEach(box => box.classList.remove('selected-for-merge'));

        syncCacheToServer(sourceImage).then(success => {
            if (!success) {
                logDebug("Warning: Failed to sync merge changes to server cache");
            }
        });
    }

    // Editable Text Box States
    function enterEditMode(textBox, sourceImage) {
        if (editableState.activeEditBox) return;

        if (mergeState.anchorBox) {
            mergeState.anchorBox.classList.remove('selected-for-merge');
            mergeState.anchorBox = null;
        }

        editableState.activeEditBox = textBox;
        editableState.originalText = textBox.dataset.fullText;

        editableState.originalStyles = {
            background: textBox.style.background,
            color: textBox.style.color,
            zIndex: textBox.style.zIndex,
            whiteSpace: textBox.style.whiteSpace,
            textAlign: textBox.style.textAlign,
            overflow: textBox.style.overflow,
            padding: textBox.style.padding,
            display: textBox.style.display,
            wordWrap: textBox.style.wordWrap,
            pointerEvents: textBox.style.pointerEvents
        };

        textBox.contentEditable = 'true';
        textBox.classList.add('editing');

        textBox.textContent = editableState.originalText.replace(/\u200B/g, "\n");

        Object.assign(textBox.style, {
            background: 'rgba(255, 255, 255, 0.95)',
            color: '#000',
            zIndex: '10000',
            border: '2px solid #3498db',
            borderRadius: '4px',
            padding: '0px',
            whiteSpace: 'pre',
            textAlign: 'left',
            overflow: 'auto',
            overflowWrap: 'normal',
            wordWrap: 'normal',
            cursor: 'text',
            outline: 'none',
            resize: 'none',
            minWidth: 'max-content',
            minHeight: 'max-content',
            pointerEvents: 'auto'
        });

        textBox.focus();
        const range = document.createRange();
        range.selectNodeContents(textBox);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);

        const saveEdit = () => {
            if (!editableState.originalText) return;

            const newText = textBox.textContent.trim().replace(/\n+/g, '\n');

            if (newText && newText !== editableState.originalText.replace(/\u200B/g, "\n")) {
                saveTextChanges(textBox, sourceImage, newText);
            }
            exitEditMode(textBox);
        };

        const cancelEdit = () => {
            exitEditMode(textBox, true);
        };

        textBox.addEventListener('blur', saveEdit, { once: true });

        textBox.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                textBox.removeEventListener('blur', saveEdit);
                saveEdit();
            } else if (e.key === 'Escape') {
                e.preventDefault();
                textBox.removeEventListener('blur', saveEdit);
                cancelEdit();
            }
        });

        const state = managedElements.get(sourceImage);
        if (state && state.hideTimer) {
            clearTimeout(state.hideTimer);
            state.hideTimer = null;
        }
    }

    function exitEditMode(textBox, restoreOriginal = false) {
        if (!textBox) return;

        if (restoreOriginal && editableState.originalText) {
            const hasLineBreaks = editableState.originalText.includes('\u200B');
            if (hasLineBreaks) {
                textBox.innerHTML = editableState.originalText.replace(/\u200B/g, "<br>");
            } else {
                textBox.textContent = editableState.originalText;
            }
        }

        textBox.contentEditable = 'false';
        textBox.classList.remove('editing');
        textBox.style.border = '';
        textBox.style.borderRadius = '';
        textBox.style.overflowWrap = '';
        textBox.style.wordWrap = '';

        if (editableState.originalStyles) {
            Object.assign(textBox.style, editableState.originalStyles);
        }

        editableState.activeEditBox = null;
        editableState.originalText = null;
        editableState.originalStyles = null;
    }

    function saveTextChanges(textBox, sourceImage, newText) {
        const normalizedText = newText.replace(/\n/g, '\u200B');

        textBox.dataset.fullText = normalizedText;

        const data = ocrDataCache.get(sourceImage);
        if (data && Array.isArray(data)) {
            const index = textBox._ocrDataIndex;
            if (data[index]) {
                data[index].text = normalizedText;
                data[index].isMerged = normalizedText.includes('\u200B');
                ocrDataCache.set(sourceImage, data);

                textBox._ocrData = data[index];

                if (data[index].isMerged) {
                    textBox.style.whiteSpace = 'pre';
                    textBox.style.textAlign = 'start';
                    textBox.innerHTML = normalizedText.replace(/\u200B/g, "<br>");
                } else {
                    textBox.style.whiteSpace = 'nowrap';
                    textBox.style.textAlign = 'center';
                    textBox.textContent = normalizedText;
                }

                logDebug(`Updated OCR text for box ${index}: "${normalizedText.substring(0, 50)}${normalizedText.length > 50 ? '...' : ''}"`);
            }
        }

        const overlay = textBox.closest('.gemini-ocr-decoupled-overlay');
        if (overlay) overlay._forceRecalc = true;
        const imgRect = sourceImage.getBoundingClientRect();
        calculateAndApplyStylesForSingleBox(textBox, imgRect);

        syncCacheToServer(sourceImage).then(success => {
            if (!success) {
                logDebug("Warning: Failed to sync edit changes to server cache");
            }
        });
    }

    function displayOcrResults(targetImg) {
        if (managedElements.has(targetImg)) return;
        const data = ocrDataCache.get(targetImg);
        if (!data || data === 'pending' || !Array.isArray(data)) return;

        const overlay = document.createElement('div');
        overlay.className = `gemini-ocr-decoupled-overlay interaction-mode-${settings.interactionMode}`;
        overlay.classList.toggle('solo-hover-mode', settings.soloHoverMode);

        if (isOcrPaused) {
            overlay.style.display = 'none';
        }

        data.forEach((item, index) => {
            const ocrBox = document.createElement('div');
            ocrBox.className = 'gemini-ocr-text-box';
            ocrBox.dataset.fullText = item.text;
            ocrBox.dataset.originalText = item.text;
            ocrBox._ocrData = item;
            ocrBox._ocrDataIndex = index;

            ocrBox.textContent = item.text.replace(/\u200B/g, "\n");

            if (item.isMerged) {
                ocrBox.style.whiteSpace = 'pre';
                ocrBox.style.textAlign = 'start';
            } else {
                ocrBox.style.whiteSpace = 'nowrap';
                ocrBox.style.textAlign = 'center';
            }

            Object.assign(ocrBox.style, {
                left:   `${item.tightBoundingBox.x * 100}%`,
                top:    `${item.tightBoundingBox.y * 100}%`,
                width:  `${item.tightBoundingBox.width * 100}%`,
                height: `${item.tightBoundingBox.height * 100}%`
            });

            overlay.appendChild(ocrBox);
        });

        document.body.appendChild(overlay);
        const state = { overlay, lastWidth: 0, lastHeight: 0, image: targetImg, hideTimer: null };
        managedElements.set(targetImg, state);

        const handleShow = () => {
            if (isOcrPaused) return;
            if (state.hideTimer) {
                clearTimeout(state.hideTimer);
                state.hideTimer = null;
            }
            showOverlay(overlay, targetImg);
        };

        const handleHide = () => {
            if (activeOverlay && activeOverlay !== overlay) return;
            state.hideTimer = setTimeout(() => {
                if (activeOverlay === overlay && mergeState.anchorBox === null && !editableState.activeEditBox) {
                    hideActiveOverlay();
                }
                state.hideTimer = null;
            }, 500);
        };

        const resetToolState = () => {
            toolState.isMergeMode = false;
            toolState.isEditMode = false;
            toolState.isDeleteMode = false;
            if (UI.fabMerge) UI.fabMerge.classList.remove('active-mode');
            if (UI.fabEdit) UI.fabEdit.classList.remove('active-mode');
            if (UI.fabDelete) UI.fabDelete.classList.remove('active-mode');
            if (mergeState.anchorBox) {
                 mergeState.anchorBox.classList.remove('selected-for-merge');
                 mergeState.anchorBox = null;
            }
        };

        const cancelSelection = () => {
            overlay.querySelectorAll('.manual-highlight, .selected-for-merge')
                .forEach(b => b.classList.remove('manual-highlight', 'selected-for-merge'));
            overlay.classList.remove('has-manual-highlight');
            mergeState.anchorBox = null;
        };

        const processBoxClick = (clickedBox, e) => {
            const isDelete = isModifierPressed(e, settings.deleteModifierKey) || toolState.isDeleteMode;
            const isMerge = isModifierPressed(e, settings.mergeModifierKey) || toolState.isMergeMode;
            const isEdit = toolState.isEditMode;

            if (isDelete) {
                if (toolState.isDeleteMode) {
                    clickedBox.classList.add('selected-for-merge');
                    setTimeout(() => {
                        if (confirm("Delete this text box?")) {
                            handleBoxDelete(clickedBox, targetImg);
                            resetToolState();
                        } else {
                            clickedBox.classList.remove('selected-for-merge');
                        }
                    }, 10);
                } else {
                    handleBoxDelete(clickedBox, targetImg);
                }
            } else if (isMerge) {
                if (!mergeState.anchorBox) {
                    mergeState.anchorBox = clickedBox;
                    clickedBox.classList.add('selected-for-merge');
                    logDebug('Merge anchor selected: ' + clickedBox.dataset.fullText);
                } else if (mergeState.anchorBox !== clickedBox) {
                    if (toolState.isMergeMode) {
                         clickedBox.classList.add('selected-for-merge');
                         setTimeout(() => {
                            if (confirm("Merge these text boxes?")) {
                                handleBoxMerge(mergeState.anchorBox, clickedBox, targetImg, overlay);
                                resetToolState();
                            } else {
                                clickedBox.classList.remove('selected-for-merge');
                            }
                         }, 10);
                    } else {
                        handleBoxMerge(mergeState.anchorBox, clickedBox, targetImg, overlay);
                    }
                } else {
                    clickedBox.classList.remove('selected-for-merge');
                    mergeState.anchorBox = null;
                }
            } else if (isEdit) {
                enterEditMode(clickedBox, targetImg);
                resetToolState();
            } else if (settings.interactionMode === 'click') {
                overlay.querySelectorAll('.manual-highlight').forEach(b => b.classList.remove('manual-highlight'));
                clickedBox.classList.add('manual-highlight');
                overlay.classList.add('has-manual-highlight');
            }
        };

        // Desktop Hover
        targetImg.addEventListener('mouseenter', handleShow);
        targetImg.addEventListener('mouseleave', handleHide);
        overlay.addEventListener('mouseenter', handleShow);
        overlay.addEventListener('mouseleave', handleHide);

        // PC Wheel Scrolling Fix
        overlay.addEventListener('wheel', (e) => {
            if (editableState.activeEditBox) return;
            e.preventDefault();

            const scrollMultiplier = 0.85;

            let scrollTarget = window;
            let node = targetImg;

            while (node && node !== document.body && node !== document.documentElement) {
                const style = window.getComputedStyle(node);
                const canScrollY = node.scrollHeight > node.clientHeight && (style.overflowY === 'auto' || style.overflowY === 'scroll');
                const canScrollX = node.scrollWidth > node.clientWidth && (style.overflowX === 'auto' || style.overflowX === 'scroll');

                if (canScrollY || canScrollX) {
                    scrollTarget = node;
                    break;
                }
                node = node.parentNode;
            }

            const moveX = e.deltaX * scrollMultiplier;
            const moveY = e.deltaY * scrollMultiplier;

            if (scrollTarget === window) {
                window.scrollBy({ left: moveX, top: moveY });
            } else {
                scrollTarget.scrollBy({ left: moveX, top: moveY });
            }
        }, { passive: false });

        // PC Click / DblClick
        overlay.addEventListener('click', (e) => {
            if (editableState.activeEditBox) return;
            const clickedBox = e.target.closest('.gemini-ocr-text-box');
            if (!clickedBox) {
                cancelSelection();
                return;
            }
            e.stopPropagation();
            e.preventDefault();
            processBoxClick(clickedBox, e);
        });

        overlay.addEventListener('dblclick', (e) => {
            if (editableState.activeEditBox) return;
            const clickedBox = e.target.closest('.gemini-ocr-text-box');
            if (clickedBox) {
                e.stopPropagation();
                enterEditMode(clickedBox, targetImg);
            }
        });

        // Mobile Touch & Click
        targetImg.addEventListener('touchstart', handleShow, { passive: true });

        targetImg.addEventListener('click', (e) => {
            handleShow();
            if (editableState.activeEditBox) return;

            let clickedBox = null;
            const boxes = overlay.querySelectorAll('.gemini-ocr-text-box');
            const padding = 15;

            for (let i = 0; i < boxes.length; i++) {
                const box = boxes[i];
                const rect = box.getBoundingClientRect();
                if (e.clientX >= rect.left - padding && e.clientX <= rect.right + padding &&
                    e.clientY >= rect.top - padding && e.clientY <= rect.bottom + padding) {
                    clickedBox = box;
                    break;
                }
            }

            if (clickedBox) {
                e.stopPropagation();
                e.preventDefault();
                processBoxClick(clickedBox, e);
            } else {
                cancelSelection();
            }
        });

        resizeObserver.observe(targetImg);
        intersectionObserver.observe(targetImg);
    }

    // --- Anki & Batch Processing ---
    function showImageSelectionDialog(validImages) {
        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            Object.assign(overlay.style, {
                position: 'fixed',
                top: '0',
                left: '0',
                width: '100vw',
                height: '100vh',
                background: 'rgba(0, 0, 0, 0.8)',
                zIndex: '10000000',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                padding: '20px',
                boxSizing: 'border-box'
            });

            const title = document.createElement('div');
            title.textContent = 'Select which page to crop:';
            Object.assign(title.style, {
                color: '#fff',
                fontSize: '24px',
                fontWeight: 'bold',
                marginBottom: '20px',
                textAlign: 'center'
            });
            overlay.appendChild(title);

            const wrapper = document.createElement('div');
            Object.assign(wrapper.style, {
                display: 'flex',
                justifyContent: 'center',
                width: '100%',
                maxWidth: '90vw'
            });

            const container = document.createElement('div');
            Object.assign(container.style, {
                display: 'flex',
                gap: '20px',
                flexWrap: 'nowrap',
                justifyContent: 'center',
                alignItems: 'center',
                maxWidth: '90vw',
                maxHeight: '70vh',
                overflowX: 'auto',
                overflowY: 'hidden',
                scrollbarWidth: 'none',
                msOverflowStyle: 'none',
                padding: '10px 20px',
                width: 'auto',
                margin: '0 auto'
            });
            container.classList.add('image-selector-container');

            validImages.forEach((item) => {
                const card = document.createElement('div');
                Object.assign(card.style, {
                    background: '#2a2a2e',
                    borderRadius: '10px',
                    padding: '15px',
                    cursor: 'pointer',
                    transition: 'all 0.2s',
                    border: '3px solid transparent',
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    minWidth: '200px',
                    maxWidth: '400px',
                    flexShrink: 0
                });

                const thumbnail = document.createElement('canvas');
                const maxThumbHeight = 480;
                const scale = Math.min(
                    maxThumbHeight / item.image.naturalHeight,
                    1
                );
                const displayScale = 2;
                thumbnail.width = item.image.naturalWidth * scale * displayScale;
                thumbnail.height = item.image.naturalHeight * scale * displayScale;
                const ctx = thumbnail.getContext('2d');
                ctx.imageSmoothingEnabled = true;
                ctx.imageSmoothingQuality = 'high';
                ctx.drawImage(item.image, 0, 0, thumbnail.width, thumbnail.height);
                Object.assign(thumbnail.style, {
                    borderRadius: '5px',
                    marginBottom: '10px',
                    maxWidth: '100%',
                    width: `${thumbnail.width / displayScale}px`,
                    height: `${thumbnail.height / displayScale}px`,
                    maxHeight: '480px',
                    imageRendering: 'high-quality'
                });

                const label = document.createElement('div');
                label.textContent = `Page ${item.pageNumber >= 0 ? item.pageNumber : '?'}`;
                Object.assign(label.style, {
                    color: '#fff',
                    fontSize: '18px',
                    fontWeight: 'bold',
                    textAlign: 'center'
                });

                card.appendChild(thumbnail);
                card.appendChild(label);

                card.addEventListener('mouseenter', () => {
                    card.style.transform = 'scale(1.05)';
                    card.style.borderColor = '#00bfff';
                    card.style.boxShadow = '0 8px 20px rgba(0, 191, 255, 0.3)';
                });
                card.addEventListener('mouseleave', () => {
                    card.style.transform = 'scale(1)';
                    card.style.borderColor = 'transparent';
                    card.style.boxShadow = 'none';
                });

                card.addEventListener('click', () => {
                    cleanup();
                    resolve(item.image);
                });

                container.appendChild(card);
            });

            wrapper.appendChild(container);
            overlay.appendChild(wrapper);

            const cancelBtn = document.createElement('button');
            cancelBtn.textContent = 'Cancel';
            Object.assign(cancelBtn.style, {
                marginTop: '20px',
                padding: '12px 30px',
                fontSize: '16px',
                background: '#c0392b',
                color: '#fff',
                border: 'none',
                borderRadius: '6px',
                cursor: 'pointer',
                fontWeight: 'bold'
            });
            cancelBtn.addEventListener('click', () => {
                cleanup();
                resolve(null);
            });
            overlay.appendChild(cancelBtn);

            document.body.appendChild(overlay);

            function cleanup() {
                if (overlay.parentNode) document.body.removeChild(overlay);
            }
        });
    }

    async function ankiConnectRequest(action, params = {}) {
        return new Promise((resolve, reject) => {
            const requestData = {
                action: action,
                version: 6,
                params: params
            };
            GM_xmlhttpRequest({
                method: 'POST',
                url: settings.ankiConnectUrl,
                data: JSON.stringify(requestData),
                headers: {
                    'Content-Type': 'application/json; charset=UTF-8'
                },
                timeout: 15000,
                onload: (response) => {
                    try {
                        const data = JSON.parse(response.responseText);
                        if (data.error) {
                            reject(new Error(data.error));
                        } else {
                            resolve(data.result);
                        }
                    } catch (error) {
                        reject(new Error('Failed to parse Anki-Connect response.'));
                    }
                },
                onerror: () => reject(new Error('Connection to Anki-Connect failed.')),
                ontimeout: () => reject(new Error('Anki-Connect request timed out.'))
            });
        });
    }

    /**
     * Exports an image to Anki with interactive cropping (8-direction handles)
     * @param {HTMLImageElement} targetImg - The image to export
     * @returns {Promise<boolean>} True if successful, false otherwise
     */
    async function exportImageToAnki(targetImg) {
        if (!settings.ankiImageField) {
            alert('Anki Image Field is not set.');
            return false;
        }

        if (!targetImg?.complete || !targetImg.naturalHeight) {
            alert('Anki Export Failed: Image not valid.');
            return false;
        }

        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            Object.assign(overlay.style, {
                position: 'fixed',
                top: '0',
                left: '0',
                width: '100vw',
                height: '100vh',
                background: 'rgba(0, 0, 0, 0.5)',
                cursor: 'default',
                zIndex: '9999999',
                touchAction: 'none'
            });
            document.body.appendChild(overlay);

            const ASPECT_RATIO = 4 / 3;
            let isRatioLocked = true;

            const cropBox = document.createElement('div');
            Object.assign(cropBox.style, {
                position: 'absolute',
                border: '2px solid #00bfff',
                background: 'rgba(0, 191, 255, 0.2)',
                width: '300px',
                height: '225px',
                top: '50%',
                left: '50%',
                transform: 'translate(-50%, -50%)',
                boxSizing: 'border-box',
                cursor: 'move',
                touchAction: 'none'
            });
            overlay.appendChild(cropBox);

            // 8-direction handles (larger for touch)
            const handles = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
            handles.forEach(corner => {
                const handle = document.createElement('div');
                Object.assign(handle.style, {
                    position: 'absolute',
                    width: '20px',
                    height: '20px',
                    background: '#00bfff',
                    borderRadius: '50%',
                    cursor: `${corner}-resize`,
                    zIndex: '10'
                });

                if (corner.includes('n')) handle.style.top = '-10px';
                if (corner.includes('s')) handle.style.bottom = '-10px';
                if (corner.includes('w')) handle.style.left = '-10px';
                if (corner.includes('e')) handle.style.right = '-10px';

                if (corner === 'n' || corner === 's') handle.style.left = 'calc(50% - 10px)';
                if (corner === 'e' || corner === 'w') handle.style.top = 'calc(50% - 10px)';

                handle.dataset.corner = corner;
                cropBox.appendChild(handle);
            });

            const btnContainer = document.createElement('div');
            Object.assign(btnContainer.style, {
                position: 'fixed',
                bottom: '40px',
                left: '50%',
                transform: 'translateX(-50%)',
                display: 'flex',
                gap: '15px',
                zIndex: '10000000',
                width: 'max-content',
                maxWidth: '95vw',
                flexWrap: 'wrap',
                justifyContent: 'center'
            });

            const toggleRatioBtn = createButton('🔒 Locked (4:3)', '#3498db');
            const confirmBtn = createButton('✅ Confirm', '#2ecc71');
            const cancelBtn = createButton('❌ Cancel', '#c0392b');

            btnContainer.appendChild(toggleRatioBtn);
            btnContainer.appendChild(confirmBtn);
            btnContainer.appendChild(cancelBtn);
            document.body.appendChild(btnContainer);

            const dragState = {
                isDragging: false,
                isResizing: false,
                dragOffsetX: 0,
                dragOffsetY: 0,
                startX: 0,
                startY: 0,
                corner: '',
                startWidth: 0,
                startHeight: 0,
                startLeft: 0,
                startTop: 0
            };

            cropBox.addEventListener('mousedown', startDragging);
            cropBox.addEventListener('touchstart', startDraggingTouch, { passive: false });

            cropBox.querySelectorAll('[data-corner]').forEach(handle => {
                handle.addEventListener('mousedown', startResizing);
                handle.addEventListener('touchstart', startResizingTouch, { passive: false });
            });

            document.addEventListener('mousemove', handleMouseMove);
            document.addEventListener('touchmove', handleTouchMove, { passive: false });
            document.addEventListener('mouseup', handleMouseUp);
            document.addEventListener('touchend', handleTouchEnd);

            toggleRatioBtn.addEventListener('click', handleToggleRatio);
            confirmBtn.addEventListener('click', handleConfirm);
            cancelBtn.addEventListener('click', handleCancel);

            function createButton(text, bgColor) {
                const button = document.createElement('button');
                button.textContent = text;
                Object.assign(button.style, {
                    padding: '12px 20px',
                    fontSize: '16px',
                    background: bgColor,
                    color: '#fff',
                    border: 'none',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    touchAction: 'manipulation',
                    fontWeight: 'bold',
                    boxShadow: '0 4px 6px rgba(0,0,0,0.3)'
                });
                return button;
            }

            function handleToggleRatio() {
                isRatioLocked = !isRatioLocked;
                if (isRatioLocked) {
                    toggleRatioBtn.textContent = '🔒 Locked (4:3)';
                    const currentWidth = parseFloat(cropBox.style.width) || cropBox.offsetWidth;
                    const newHeight = currentWidth / ASPECT_RATIO;
                    cropBox.style.height = `${newHeight}px`;
                } else {
                    toggleRatioBtn.textContent = '🔓 Free Crop';
                }
            }

            function startDragging(e) {
                if (e.target.dataset.corner) return;
                const rect = cropBox.getBoundingClientRect();
                dragState.dragOffsetX = e.clientX - rect.left;
                dragState.dragOffsetY = e.clientY - rect.top;
                dragState.isDragging = true;
                e.preventDefault();
            }

            function startDraggingTouch(e) {
                if (e.target.dataset.corner) return;
                e.preventDefault();
                const touch = e.touches[0];
                const rect = cropBox.getBoundingClientRect();
                dragState.dragOffsetX = touch.clientX - rect.left;
                dragState.dragOffsetY = touch.clientY - rect.top;
                dragState.isDragging = true;
            }

            function startResizing(e) {
                e.stopPropagation();
                dragState.isResizing = true;
                dragState.corner = e.target.dataset.corner;
                dragState.startX = e.clientX;
                dragState.startY = e.clientY;
                const rect = cropBox.getBoundingClientRect();
                dragState.startWidth = rect.width;
                dragState.startHeight = rect.height;
                dragState.startLeft = rect.left;
                dragState.startTop = rect.top;
                e.preventDefault();
            }

            function startResizingTouch(e) {
                e.stopPropagation();
                e.preventDefault();
                const touch = e.touches[0];
                dragState.isResizing = true;
                dragState.corner = e.target.dataset.corner;
                dragState.startX = touch.clientX;
                dragState.startY = touch.clientY;
                const rect = cropBox.getBoundingClientRect();
                dragState.startWidth = rect.width;
                dragState.startHeight = rect.height;
                dragState.startLeft = rect.left;
                dragState.startTop = rect.top;
            }

            function handleMouseMove(e) {
                if (dragState.isDragging) handleDrag(e.clientX, e.clientY);
                else if (dragState.isResizing) handleResize(e.clientX, e.clientY, e.shiftKey);
            }

            function handleTouchMove(e) {
                if (dragState.isDragging || dragState.isResizing) {
                    e.preventDefault();
                    const touch = e.touches[0];
                    if (dragState.isDragging) handleDrag(touch.clientX, touch.clientY);
                    else handleResize(touch.clientX, touch.clientY, false);
                }
            }

            function handleDrag(clientX, clientY) {
                const newX = clientX - dragState.dragOffsetX;
                const newY = clientY - dragState.dragOffsetY;
                const maxX = window.innerWidth - cropBox.offsetWidth;
                const maxY = window.innerHeight - cropBox.offsetHeight;
                cropBox.style.left = Math.max(0, Math.min(newX, maxX)) + 'px';
                cropBox.style.top = Math.max(0, Math.min(newY, maxY)) + 'px';
                cropBox.style.transform = 'none';
            }

            function handleResize(clientX, clientY, shiftKey) {
                const deltaX = clientX - dragState.startX;
                const deltaY = clientY - dragState.startY;
                let newWidth = dragState.startWidth;
                let newHeight = dragState.startHeight;
                let newLeft = dragState.startLeft;
                let newTop = dragState.startTop;
                const corner = dragState.corner;

                if (corner.includes('e')) newWidth = Math.max(50, dragState.startWidth + deltaX);
                if (corner.includes('w')) {
                    newWidth = Math.max(50, dragState.startWidth - deltaX);
                    newLeft = dragState.startLeft + (dragState.startWidth - newWidth);
                }
                if (corner.includes('s')) newHeight = Math.max(50, dragState.startHeight + deltaY);
                if (corner.includes('n')) {
                    newHeight = Math.max(50, dragState.startHeight - deltaY);
                    newTop = dragState.startTop + (dragState.startHeight - newHeight);
                }

                const effectivelyLocked = isRatioLocked ? !shiftKey : shiftKey;

                if (effectivelyLocked) {
                    if (corner === 'n' || corner === 's') {
                        const targetWidth = newHeight * ASPECT_RATIO;
                        newLeft = dragState.startLeft - (targetWidth - dragState.startWidth) / 2;
                        newWidth = targetWidth;
                    } else if (corner === 'e' || corner === 'w') {
                        const targetHeight = newWidth / ASPECT_RATIO;
                        newTop = dragState.startTop - (targetHeight - dragState.startHeight) / 2;
                        newHeight = targetHeight;
                    } else {
                        newHeight = newWidth / ASPECT_RATIO;
                        if (corner.includes('n')) newTop = dragState.startTop + (dragState.startHeight - newHeight);
                    }
                }

                if (newLeft < 0) {
                    newWidth += newLeft;
                    newLeft = 0;
                    if (effectivelyLocked) { newHeight = newWidth / ASPECT_RATIO; if(corner.includes('n')) newTop = dragState.startTop + (dragState.startHeight - newHeight); }
                }
                if (newTop < 0) {
                    newHeight += newTop;
                    newTop = 0;
                    if (effectivelyLocked) { newWidth = newHeight * ASPECT_RATIO; if(corner.includes('w')) newLeft = dragState.startLeft + (dragState.startWidth - newWidth); }
                }
                if (newLeft + newWidth > window.innerWidth) {
                    newWidth = window.innerWidth - newLeft;
                    if (effectivelyLocked) { newHeight = newWidth / ASPECT_RATIO; if(corner.includes('n')) newTop = dragState.startTop + (dragState.startHeight - newHeight); }
                }
                if (newTop + newHeight > window.innerHeight) {
                    newHeight = window.innerHeight - newTop;
                    if (effectivelyLocked) { newWidth = newHeight * ASPECT_RATIO; if(corner.includes('w')) newLeft = dragState.startLeft + (dragState.startWidth - newWidth); }
                }

                cropBox.style.width = newWidth + 'px';
                cropBox.style.height = newHeight + 'px';
                cropBox.style.left = newLeft + 'px';
                cropBox.style.top = newTop + 'px';
                cropBox.style.transform = 'none';
            }

            function handleMouseUp() {
                dragState.isDragging = false;
                dragState.isResizing = false;
            }

            function handleTouchEnd() {
                dragState.isDragging = false;
                dragState.isResizing = false;
            }

            async function handleConfirm() {
                const cropRect = cropBox.getBoundingClientRect();
                const imgRect = targetImg.getBoundingClientRect();
                const scaleX = targetImg.naturalWidth / targetImg.offsetWidth;
                const scaleY = targetImg.naturalHeight / targetImg.offsetHeight;
                const cropData = {
                    x: Math.max(0, (cropRect.left - imgRect.left) * scaleX),
                    y: Math.max(0, (cropRect.top - imgRect.top) * scaleY),
                    width: Math.min(cropRect.width * scaleX, targetImg.naturalWidth),
                    height: Math.min(cropRect.height * scaleY, targetImg.naturalHeight)
                };
                const canvas = document.createElement('canvas');
                canvas.width = cropData.width;
                canvas.height = cropData.height;
                const ctx = canvas.getContext('2d', { alpha: false });
                ctx.drawImage(
                    targetImg,
                    cropData.x, cropData.y, cropData.width, cropData.height,
                    0, 0, cropData.width, cropData.height
                );
                try {
                    const blob = await new Promise((resolve, reject) => {
                        canvas.toBlob((blob) => {
                            if (!blob) reject(new Error('Failed to crop image'));
                            else resolve(blob);
                        }, 'image/jpeg', 0.99);
                    });
                    const success = await processAndSendToAnki(blob);
                    cleanup();
                    resolve(success);
                } catch (error) {
                    console.error('Cropping error:', error);
                    alert('Error cropping image: ' + error.message);
                    cleanup();
                    resolve(false);
                }
            }

            async function processAndSendToAnki(blob) {
                return new Promise((resolve) => {
                    const reader = new FileReader();
                    reader.onload = async () => {
                        const base64Data = reader.result.split(',')[1];
                        try {
                            const notes = await ankiConnectRequest("findNotes", { query: "added:1" });
                            if (!notes || notes.length === 0) throw new Error("No recently added cards found. Create one first.");
                            const lastNoteId = notes.sort((a, b) => b - a)[0];
                            const filename = `mangatan_${lastNoteId}.jpg`;
                            await ankiConnectRequest("updateNoteFields", {
                                note: {
                                    id: lastNoteId,
                                    fields: { [settings.ankiImageField]: "" },
                                    picture: {
                                        filename: filename,
                                        data: base64Data,
                                        fields: [settings.ankiImageField]
                                    }
                                }
                            });
                            resolve(true);
                        } catch (error) {
                            console.error("Anki error:", error);
                            alert("Error sending to Anki: " + error.message);
                            resolve(false);
                        }
                    };
                    reader.onerror = () => {
                        console.error("FileReader error");
                        alert("Error reading image data");
                        resolve(false);
                    };
                    reader.readAsDataURL(blob);
                });
            }

            function handleCancel() {
                cleanup();
                resolve(false);
            }

            function cleanup() {
                document.removeEventListener('mousemove', handleMouseMove);
                document.removeEventListener('touchmove', handleTouchMove);
                document.removeEventListener('mouseup', handleMouseUp);
                document.removeEventListener('touchend', handleTouchEnd);
                if (overlay.parentNode) document.body.removeChild(overlay);
                if (btnContainer.parentNode) document.body.removeChild(btnContainer);
            }
        });
    }

    // --- Sync Cache ---
    async function syncCacheToServer(sourceImage) {
        if (!sourceImage || !sourceImage.src) {
            logDebug("syncCacheToServer: Invalid image provided");
            return false;
        }
        const cacheData = ocrDataCache.get(sourceImage);
        if (!cacheData || !Array.isArray(cacheData)) {
            logDebug("syncCacheToServer: No valid cache data found");
            return false;
        }

        const normalizedUrl = sourceImage.src.split('?')[0];

        return new Promise((resolve) => {
            const updateData = {
                url: normalizedUrl,
                data: cacheData,
                context: document.title
            };
            GM_xmlhttpRequest({
                method: 'POST',
                url: `${settings.ocrServerUrl}/update-cache`,
                headers: { 'Content-Type': 'application/json' },
                data: JSON.stringify(updateData),
                timeout: 10000,
                onload: (response) => {
                    try {
                        const data = JSON.parse(response.responseText);
                        if (response.status === 200 && data.status === 'success') {
                            logDebug(`Cache synced successfully: ${normalizedUrl.slice(-50)}`);
                            resolve(true);
                        } else {
                            throw new Error(data.error || 'Unknown error');
                        }
                    } catch (e) {
                        logDebug(`Cache sync failed: ${e.message}`);
                        resolve(false);
                    }
                },
                onerror: () => { logDebug('Cache sync connection error'); resolve(false); },
                ontimeout: () => { logDebug('Cache sync timed out'); resolve(false); }
            });
        });
    }

    // --- Batch & Chapter Processing ---
    async function runProbingProcess(baseUrl, btn) {
        logDebug(`Requesting SERVER-SIDE job for: ${baseUrl}`);
        const originalText = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Starting...';
        const postData = { baseUrl: baseUrl, user: settings.imageServerUser, pass: settings.imageServerPassword, context: document.title };
        GM_xmlhttpRequest({
            method: 'POST',
            url: `${settings.ocrServerUrl}/preprocess-chapter`,
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify(postData),
            timeout: 10000,
            onload: (res) => {
                try {
                    const data = JSON.parse(res.responseText);
                    if (res.status === 202 && data.status === 'accepted') {
                        btn.textContent = 'Accepted';
                        btn.style.borderColor = '#3498db';
                        checkServerStatus();
                    } else {
                        throw new Error(data.error || `Server responded with status ${res.status}`);
                    }
                } catch (e) {
                    logDebug(`Error starting chapter job: ${e.message}`);
                    btn.textContent = 'Error!';
                    btn.style.borderColor = '#c032b';
                    alert(`Failed to start chapter job: ${e.message}`);
                }
            },
            onerror: () => {
                logDebug('Connection error on chapter job.');
                btn.textContent = 'Conn. Error!';
                btn.style.borderColor = '#c0392b';
                alert('Failed to connect to the OCR server to start the job.');
            },
            ontimeout: () => {
                logDebug('Timeout on chapter job.');
                btn.textContent = 'Timeout!';
                btn.style.borderColor = '#c0392b';
                alert('The request to start the chapter job timed out.');
            },
            onloadend: () => {
                setTimeout(() => {
                    if (btn.isConnected) {
                        btn.textContent = originalText;
                        btn.style.borderColor = '';
                        btn.disabled = false;
                    }
                }, 3500);
            }
        });
    }

    async function batchProcessCurrentChapterFromURL() {
        const urlMatch = window.location.pathname.match(/\/manga\/\d+\/chapter\/\d+/);
        if (!urlMatch) return alert(`Error: URL does not match '.../manga/ID/chapter/ID'.`);
        await runProbingProcess(`${window.location.origin}/api/v1${urlMatch[0]}/page/`, UI.batchChapterBtn);
    }

    async function handleChapterBatchClick(event) {
        event.preventDefault();
        event.stopPropagation();
        const chapterLink = event.currentTarget.closest('a[href*="/manga/"][href*="/chapter/"]');
        if (!chapterLink?.href) return;
        const urlPath = new URL(chapterLink.href).pathname;
        await runProbingProcess(`${window.location.origin}/api/v1${urlPath}/page/`, event.currentTarget);
    }

    function addOcrButtonToChapter(chapterLinkElement) {
        const moreButton = chapterLinkElement.querySelector('button[aria-label="more"]');
        if (!moreButton) return;
        const actionContainer = moreButton.parentElement;
        if (!actionContainer || actionContainer.querySelector('.gemini-ocr-chapter-batch-btn')) return;
        const ocrButton = document.createElement('button');
        ocrButton.textContent = 'OCR';
        ocrButton.className = 'gemini-ocr-chapter-batch-btn';
        ocrButton.title = 'Queue this chapter for background pre-processing on the server';
        ocrButton.addEventListener('click', handleChapterBatchClick);
        actionContainer.insertBefore(ocrButton, moreButton);
    }

    // --- UI, Styles and Initialization ---
    function applyTheme() {
        const theme = COLOR_THEMES[settings.colorTheme] || COLOR_THEMES.blue;
        const cssVars = `:root {
            --accent: ${theme.accent||'72,144,255'};
            --background: ${theme.background||'229,243,255'};
            --modal-header-color: rgba(${theme.accent||'72,144,255'}, 1);
            --ocr-dimmed-opacity: ${settings.dimmedOpacity};
            --ocr-focus-scale: ${settings.focusScaleMultiplier};
        }`;
        let styleTag = document.getElementById('gemini-ocr-dynamic-styles');
        if (!styleTag) {
            styleTag = document.createElement('style');
            styleTag.id = 'gemini-ocr-dynamic-styles';
            document.head.appendChild(styleTag);
        }
        styleTag.textContent = cssVars;

        document.body.className = document.body.className.replace(/\bocr-theme-\S+/g, '');
        document.body.classList.add(`ocr-theme-${settings.colorTheme}`);
        document.body.classList.toggle('ocr-brightness-dark', settings.brightnessMode === 'dark');
        document.body.classList.toggle('ocr-brightness-light', settings.brightnessMode === 'light');
        document.body.className = document.body.className.replace(/\bocr-focus-color-mode-\S+/g, '');
        if (settings.focusFontColor && settings.focusFontColor !== 'default') {
            document.body.classList.add(`ocr-focus-color-mode-${settings.focusFontColor}`);
        }

        document.body.classList.toggle('mobile-mode', settings.mobileMode);
    }

    function createUI() {
        GM_addStyle(`
            /* --- GHOST TEXT: Suwayomi UI Fix --- */
            .yomitan-ghost-text { visibility: hidden !important; position: relative !important; display: inherit !important; }
            .yomitan-ghost-text::after {
                content: attr(data-text); visibility: visible !important; position: absolute; top: 0; left: 0;
                width: 100%; height: 100%; font: inherit; color: inherit; letter-spacing: inherit; text-align: inherit;
                white-space: pre-wrap; pointer-events: auto;
            }
            /* --- OCR STYLES --- */
            .gemini-ocr-decoupled-overlay { position: fixed; z-index: 9998; pointer-events: none; opacity: 0; display: none; }
            .gemini-ocr-decoupled-overlay.is-focused { opacity: 1; display: block; }
            ::selection { background-color: rgba(var(--accent), 1); color: #FFFFFF; }
            .gemini-ocr-text-box {
                position: absolute; display: flex; align-items: center; justify-content: center; text-align: center;
                box-sizing: border-box; user-select: text; cursor: pointer; transition: opacity 0.2s, transform 0.1s;
                overflow: visible !important; font-family: 'Noto Sans JP', sans-serif; font-weight: 600;
                padding: 2px !important; border-radius: 4px; border: none; text-shadow: none;
                pointer-events: auto; min-height: 30px !important; min-width: 30px !important;
            }

            @media (hover: none) and (pointer: coarse) {
                .gemini-ocr-text-box { pointer-events: auto !important; touch-action: pan-x pan-y !important; }
            }

            .interaction-mode-hover.is-focused .gemini-ocr-text-box:hover,
            .interaction-mode-click.is-focused .manual-highlight {
                z-index: 1; transform: scale(var(--ocr-focus-scale, 1.1)) !important; overflow: visible !important;
                min-height: 40px !important; min-width: 40px !important;
            }

            @media (max-width: 768px) {
                .gemini-ocr-text-box { min-width: 40px !important; min-height: 40px !important; font-size: 20px !important; }
                #gemini-ocr-settings-button, #gemini-ocr-pause-button, .gemini-ocr-fab-btn { width: 45px; height: 45px; font-size: 24px; }
            }
            .gemini-ocr-text-box.selected-for-merge { outline: 3px solid #f1c40f !important; outline-offset: 2px; box-shadow: 0 0 12px #f1c40f !important; z-index: 2; }
            body.ocr-brightness-light .gemini-ocr-text-box { background: rgba(var(--background), 1); color: rgba(var(--accent), 0.5); box-shadow: 0 0 0 0.1em rgba(var(--background), 1); }
            body.ocr-brightness-light .interaction-mode-hover.is-focused .gemini-ocr-text-box:hover,
            body.ocr-brightness-light .interaction-mode-click.is-focused .manual-highlight { background: rgba(var(--background), 1); color: rgba(var(--accent), 1); box-shadow: 0 0 0 0.1em rgba(var(--background), 1), 0 0 0 0.2em rgba(var(--accent), 1); }
            body.ocr-brightness-dark .gemini-ocr-text-box { background: rgba(29, 34, 39, 0.9); color: rgba(var(--background), 0.7); box-shadow: 0 0 0 0.1em rgba(var(--accent), 0.4); backdrop-filter: blur(2px); }
            body.ocr-brightness-dark .interaction-mode-hover.is-focused .gemini-ocr-text-box:hover,
            body.ocr-brightness-dark .interaction-mode-click.is-focused .manual-highlight { background: rgba(var(--accent), 1); color: #FFFFFF; box-shadow: 0 0 0 0.1em rgba(var(--accent), 0.4), 0 0 0 0.2em rgba(var(--background), 1); }
            .ocr-focus-color-mode-black .interaction-mode-hover.is-focused .gemini-ocr-text-box:hover,
            .ocr-focus-color-mode-black .interaction-mode-click.is-focused .manual-highlight { color: #000000 !important; text-shadow: 0 0 2px #FFFFFF, 0 0 4px #FFFFFF; }
            .ocr-focus-color-mode-white .interaction-mode-hover.is-focused .gemini-ocr-text-box:hover,
            .ocr-focus-color-mode-white .interaction-mode-click.is-focused .manual-highlight { color: #FFFFFF !important; text-shadow: 0 0 2px #000000, 0 0 4px #000000; }
            .ocr-focus-color-mode-difference .interaction-mode-hover.is-focused .gemini-ocr-text-box:hover,
            .ocr-focus-color-mode-difference .interaction-mode-click.is-focused .manual-highlight { color: white !important; mix-blend-mode: difference; background: transparent !important; box-shadow: none !important; }
            .gemini-ocr-text-vertical { writing-mode: vertical-rl; text-orientation: upright; }

            .interaction-mode-hover.is-focused:not(.solo-hover-mode):has(.gemini-ocr-text-box:hover) .gemini-ocr-text-box:not(:hover),
            .interaction-mode-click.is-focused.has-manual-highlight .gemini-ocr-text-box:not(.manual-highlight) { opacity: var(--ocr-dimmed-opacity); }
            .solo-hover-mode.is-focused .gemini-ocr-text-box { opacity: 0; }
            .solo-hover-mode.is-focused .gemini-ocr-text-box:hover { opacity: 1; }

            /* Editable text box styles */
            .gemini-ocr-text-box.editing {
                background: rgba(255, 255, 255, 0.95) !important; color: #000 !important; z-index: 10000 !important;
                border: 2px solid #3498db !important; border-radius: 4px !important; padding: 0px !important;
                white-space: pre !important; text-align: left !important; overflow: auto !important;
                overflow-wrap: normal !important; word-wrap: normal !important; cursor: text !important;
                min-width: max-content !important; min-height: max-content !important; pointer-events: auto !important;
            }
            .gemini-ocr-text-box.editing.gemini-ocr-text-vertical { white-space: pre !important; text-align: start !important; }

            /* Chapter Batch Button */
            .gemini-ocr-chapter-batch-btn {
                font-family: "Roboto", "Helvetica", "Arial", sans-serif; font-weight: 500; font-size: 0.75rem;
                padding: 2px 8px; border-radius: 4px; border: 1px solid rgba(240, 153, 136, 0.5); color: #f09988;
                background-color: transparent; cursor: pointer; margin-right: 4px; transition: all 150ms;
                min-width: 80px; text-align: center;
            }
            .gemini-ocr-chapter-batch-btn:hover { background-color: rgba(240, 153, 136, 0.08); }
            .gemini-ocr-chapter-batch-btn:disabled { color: grey; border-color: grey; cursor: wait; }

            /* Settings Button */
            #gemini-ocr-settings-button {
                position: fixed; bottom: 10px; right: 15px; z-index: 2147483647; background: #1A1D21; color: #EAEAEA;
                border: 1px solid #555; border-radius: 50%; width: 50px; height: 50px; font-size: 26px; cursor: pointer;
                display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
                user-select: none; transition: all 0.2s ease; line-height: 1;
            }
            #gemini-ocr-settings-button:hover { background: #2A2D31; transform: scale(1.1); }

            /* Pause Button (Stacked vertically above Settings) */
            #gemini-ocr-pause-button {
                position: fixed; bottom: 64px; right: 15px; z-index: 2147483647; background: #1A1D21; color: #EAEAEA;
                border: 1px solid #555; border-radius: 50%; width: 50px; height: 50px; font-size: 24px; cursor: pointer;
                display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
                user-select: none; transition: all 0.2s ease; line-height: 1;
            }
            #gemini-ocr-pause-button:hover { background: #2A2D31; transform: scale(1.1); }
            #gemini-ocr-pause-button.is-paused { background: #c0392b !important; color: #fff !important; border-color: #e74c3c !important; box-shadow: 0 0 12px #e74c3c; }

            /* FAB Menu Container (Shifted up to sit above Pause Button) */
            .gemini-ocr-fab-container { position: fixed; bottom: 118px; right: 15px; z-index: 2147483646; display: flex; flex-direction: column-reverse; align-items: center; gap: 10px; }
            .gemini-ocr-fab-btn {
                width: 50px; height: 50px; border-radius: 50%; border: 1px solid #555; background: #1A1D21; color: #EAEAEA;
                font-size: 26px; cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,0.5); transition: all 0.2s cubic-bezier(0.175, 0.885, 0.32, 1.275);
                display: flex; align-items: center; justify-content: center; user-select: none; line-height: 1;
            }
            .gemini-ocr-fab-btn:hover { background: #27ae60; transform: scale(1.1); }
            .gemini-ocr-fab-child { transform: scale(0); opacity: 0; height: 0; margin: 0; border: 0; pointer-events: none; }
            .gemini-ocr-fab-container.is-open .gemini-ocr-fab-child { transform: scale(1); opacity: 1; height: 40px; width: 40px; pointer-events: auto; border: 1px solid #555; }
            .gemini-ocr-fab-btn.active-mode { background-color: #f1c40f !important; color: #000 !important; border-color: #fff !important; box-shadow: 0 0 10px #f1c40f; }
            .gemini-ocr-fab-container.is-open #gemini-ocr-fab-main { transform: rotate(45deg); background-color: #c0392b; }

            /* Modals & Layout */
            .gemini-ocr-modal {
                position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); background-color: #1A1D21;
                border: 1px solid var(--modal-header-color, #00BFFF); border-radius: 15px; z-index: 2147483647;
                color: #EAEAEA; font-family: sans-serif; box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.5);
                width: 600px; max-width: 90vw; max-height: 90vh; display: flex; flex-direction: column;
            }
            .gemini-ocr-modal.is-hidden { display: none; }
            .gemini-ocr-modal-header { padding: 20px 25px; border-bottom: 1px solid #444; }
            .gemini-ocr-modal-header h2 { margin: 0; color: var(--modal-header-color, #00BFFF); }
            .gemini-ocr-modal-content { padding: 10px 25px; overflow-y: auto; flex-grow: 1; }
            .gemini-ocr-modal-footer { padding: 15px 25px; border-top: 1px solid #444; display: flex; justify-content: flex-start; gap: 10px; align-items: center; }
            .gemini-ocr-modal-footer button:last-of-type { margin-left: auto; }
            .gemini-ocr-modal h3 { font-size: 1.1em; margin: 15px 0 10px 0; border-bottom: 1px solid #333; padding-bottom: 5px; color: var(--modal-header-color, #00BFFF); }
            .gemini-ocr-settings-grid { display: grid; grid-template-columns: max-content 1fr; gap: 10px 15px; align-items: center; }
            .full-width { grid-column: 1 / -1; }
            .gemini-ocr-modal input, .gemini-ocr-modal textarea, .gemini-ocr-modal select {
                width: 100%; padding: 8px; box-sizing: border-box; font-family: monospace;
                background-color: #2a2a2e; border: 1px solid #555; border-radius: 5px; color: #EAEAEA;
            }
            .gemini-ocr-modal button { padding: 10px 18px; border: none; border-radius: 5px; color: #1A1D21; cursor: pointer; font-weight: bold; }
            #gemini-ocr-server-status { padding: 10px; border-radius: 5px; text-align: center; cursor: pointer; transition: background-color 0.3s; }
            #gemini-ocr-server-status.status-ok { background-color: #27ae60; }
            #gemini-ocr-server-status.status-error { background-color: #c0392b; }
            #gemini-ocr-server-status.status-checking { background-color: #3498db; }

            .mobile-mode .gemini-ocr-text-box { transition: none !important; animation: none !important; }
            .image-selector-container::-webkit-scrollbar { display: none; }
        `);

        document.body.insertAdjacentHTML('beforeend', `
            <div id="gemini-ocr-fab-container" class="gemini-ocr-fab-container">
                <button id="gemini-ocr-fab-main" class="gemini-ocr-fab-btn" title="Menu">➕</button>
                <button id="gemini-ocr-fab-delete" class="gemini-ocr-fab-btn gemini-ocr-fab-child" title="Toggle Delete Mode (Click a box)">🗑️</button>
                <button id="gemini-ocr-fab-edit" class="gemini-ocr-fab-btn gemini-ocr-fab-child" title="Toggle Edit Mode (Single Click)">✏️</button>
                <button id="gemini-ocr-fab-merge" class="gemini-ocr-fab-btn gemini-ocr-fab-child" title="Toggle Merge Mode (Click 2 boxes)">🔗</button>
                <button id="gemini-ocr-fab-anki" class="gemini-ocr-fab-btn gemini-ocr-fab-child" title="Export Screenshot to Anki">⛏️</button>
            </div>

            <button id="gemini-ocr-pause-button" title="Pause/Resume OCR Overlay (Allows scanning native on-screen text)">⏸️</button>
            <button id="gemini-ocr-settings-button">⚙️</button>

            <div id="gemini-ocr-settings-modal" class="gemini-ocr-modal is-hidden">
                <div class="gemini-ocr-modal-header"><h2>Mangatan Settings</h2></div>
                <div class="gemini-ocr-modal-content">
                    <h3>OCR & Image Source</h3>
                    <div class="gemini-ocr-settings-grid full-width">
                        <label for="gemini-ocr-server-url">OCR Server URL:</label><input type="text" id="gemini-ocr-server-url">
                        <label for="gemini-image-server-user">Image Source Username:</label><input type="text" id="gemini-image-server-user" autocomplete="username" placeholder="Optional">
                        <label for="gemini-image-server-password">Image Source Password:</label><input type="password" id="gemini-image-server-password" autocomplete="current-password" placeholder="Optional">
                    </div>
                    <div id="gemini-ocr-server-status" class="full-width" style="margin-top: 10px;">Click to check server status</div>

                    <h3>Anki Integration</h3>
                    <div class="gemini-ocr-settings-grid">
                        <label for="gemini-ocr-anki-url">Anki-Connect URL:</label><input type="text" id="gemini-ocr-anki-url">
                        <label for="gemini-ocr-anki-field">Image Field Name:</label><input type="text" id="gemini-ocr-anki-field" placeholder="e.g., Image">
                    </div>

                    <h3>Interaction & Display</h3>
                    <div class="gemini-ocr-settings-grid">
                        <label for="ocr-brightness-mode">Theme Mode:</label><select id="ocr-brightness-mode"><option value="light">Light</option><option value="dark">Dark</option></select>
                        <label for="ocr-color-theme">Color Theme:</label><select id="ocr-color-theme">${Object.keys(COLOR_THEMES).map(t => `<option value="${t}">${t.charAt(0).toUpperCase()+t.slice(1)}</option>`).join('')}</select>
                        <label for="ocr-interaction-mode">Highlight Mode:</label><select id="ocr-interaction-mode"><option value="hover">On Hover</option><option value="click">On Click</option></select>
                        <label for="ocr-focus-font-color">Focus Font Color:</label><select id="ocr-focus-font-color"><option value="default">Default</option><option value="black">Black</option><option value="white">White</option><option value="difference">Difference (Blend)</option></select>
                        <label for="ocr-dimmed-opacity">Dimmed Box Opacity (%):</label><input type="number" id="ocr-dimmed-opacity" min="0" max="100" step="5">
                        <label for="ocr-focus-scale-multiplier">Focus Scale Multiplier:</label><input type="number" id="ocr-focus-scale-multiplier" min="1" max="3" step="0.05">
                        <label for="ocr-delete-key">Delete Modifier Key:</label><input type="text" id="ocr-delete-key" placeholder="Control, Alt, Shift...">
                        <label for="ocr-merge-key">Merge Modifier Key:</label><input type="text" id="ocr-merge-key" placeholder="Control, Alt, Shift...">
                        <label for="ocr-text-orientation">Text Orientation:</label><select id="ocr-text-orientation"><option value="smart">Smart</option><option value="forceHorizontal">Horizontal</option><option value="forceVertical">Vertical</option></select>
                        <label for="ocr-font-multiplier-horizontal">H. Font Multiplier:</label><input type="number" id="ocr-font-multiplier-horizontal" min="0.1" max="5" step="0.1">
                        <label for="ocr-font-multiplier-vertical">V. Font Multiplier:</label><input type="number" id="ocr-font-multiplier-vertical" min="0.1" max="5" step="0.1">
                        <label for="ocr-bounding-box-adjustment-input">Box Adjustment (px):</label><input type="number" id="ocr-bounding-box-adjustment-input" min="0" max="100" step="1">
                    </div>
                    <div class="gemini-ocr-settings-grid full-width">
                        <label><input type="checkbox" id="gemini-ocr-solo-hover-mode"> Only show hovered box (Hover Mode)</label>
                        <label><input type="checkbox" id="gemini-ocr-add-space-on-merge"> Add space on merge</label>
                    </div>
                    <div class="gemini-ocr-settings-grid full-width">
                        <label><input type="checkbox" id="gemini-ocr-mobile-mode"> Mobile Mode (Disable Animations)</label>
                    </div>
                    <div class="gemini-ocr-settings-grid full-width">
                        <label><input type="checkbox" id="gemini-ocr-mobile-edit-merging"> Enable Mobile Menu (FAB)</label>
                    </div>

                    <h3>Advanced</h3>
                    <div class="gemini-ocr-settings-grid full-width"><label><input type="checkbox" id="gemini-ocr-debug-mode"> Debug Mode</label></div>
                    <div class="gemini-ocr-settings-grid full-width">
                        <label for="gemini-ocr-sites-config">Site Configurations (URL; OverflowFix; Containers...)</label>
                        <textarea id="gemini-ocr-sites-config" rows="6" placeholder="127.0.0.1; .overflow-fix; .container1; .container2"></textarea>
                    </div>
                </div>
                <div class="gemini-ocr-modal-footer">
                    <button id="gemini-ocr-purge-cache-btn" style="background-color: #c0392b;" title="Deletes all entries from the server's OCR cache file.">Purge Server Cache</button>
                    <button id="gemini-ocr-batch-chapter-btn" style="background-color: #3498db;" title="Queues the current chapter on the server for background pre-processing.">Pre-process Chapter</button>
                    <button id="gemini-ocr-debug-btn" style="background-color: #777;">Debug</button>
                    <button id="gemini-ocr-close-btn" style="background-color: #555;">Close</button>
                    <button id="gemini-ocr-save-btn" style="background-color: #3ad602;">Save & Reload</button>
                </div>
            </div>

            <div id="gemini-ocr-debug-modal" class="gemini-ocr-modal is-hidden">
                <div class="gemini-ocr-modal-header"><h2>Debug Log</h2></div>
                <div class="gemini-ocr-modal-content"><textarea id="gemini-ocr-debug-log" readonly style="width:100%; height: 100%; resize:none;"></textarea></div>
                <div class="gemini-ocr-modal-footer"><button id="gemini-ocr-close-debug-btn" style="background-color: #555;">Close</button></div>
            </div>
        `);

        // Ghost Text Logic for Suwayomi
        function applyGhostTextToSuwayomi() {
            const isChapterPage = /\/manga\/\d+\/chapter\/\d+/.test(window.location.href);

            if (!isChapterPage) {
                const existingGhosts = document.querySelectorAll('.yomitan-ghost-text');
                if (existingGhosts.length > 0) {
                    existingGhosts.forEach(el => {
                        el.classList.remove('yomitan-ghost-text');
                        el.removeAttribute('data-text');
                    });
                }
                return;
            }

            const selectors =[
                '.MuiTypography-root', '.MuiButton-label', '.MuiButton-root',
                '.MuiChip-label', '.MuiListItemText-primary', '.MuiListItemText-secondary', '.MuiTab-wrapper'
            ];

            const textElements = document.querySelectorAll(selectors.join(', '));

            for(let i = 0; i < textElements.length; i++) {
                const el = textElements[i];

                if (el.closest('.MuiDialogContent-root') || el.childElementCount > 0) continue;

                const text = el.textContent.trim();

                if (el.classList.contains('yomitan-ghost-text')) {
                    if (text && text !== el.getAttribute('data-text')) {
                        el.setAttribute('data-text', text);
                    }
                    continue;
                }

                if (text.length === 0) continue;

                el.setAttribute('data-text', text);
                el.classList.add('yomitan-ghost-text');
            }

            const images = document.querySelectorAll('img[alt]:not([alt=""])');
            for(let i = 0; i < images.length; i++) {
                images[i].removeAttribute('alt');
            }

            const ariaBoxes = document.querySelectorAll('.MuiBox-root[aria-label]:not([aria-label=""])');
            for(let i = 0; i < ariaBoxes.length; i++) {
                ariaBoxes[i].removeAttribute('aria-label');
            }
        }

        let ghostDebounceTimer;
        const ghostObserver = new MutationObserver(() => {
            if (ghostDebounceTimer) cancelAnimationFrame(ghostDebounceTimer);
            ghostDebounceTimer = requestAnimationFrame(applyGhostTextToSuwayomi);
        });

        const appRoot = document.getElementById('root') || document.body;
        ghostObserver.observe(appRoot, { childList: true, subtree: true });

        setInterval(applyGhostTextToSuwayomi, 1500);
        applyGhostTextToSuwayomi();
    }

    const handleAnkiExportClick = async () => {
        let targetImage = activeImageForExport;
        const validRecentImages = getValidImagesForExport();

        if (validRecentImages.length > 1) {
            targetImage = await showImageSelectionDialog(validRecentImages);
            if (!targetImage) return;
        } else if (validRecentImages.length === 1) {
            targetImage = validRecentImages[0].image;
        }

        const currentChapterMatch = window.location.pathname.match(/\/manga\/\d+\/chapter\/\d+/);

        if (targetImage) {
            const imageChapterMatch = targetImage.src.match(/\/manga\/\d+\/chapter\/\d+/);
            const isFromCurrentChapter = currentChapterMatch && imageChapterMatch && currentChapterMatch[0] === imageChapterMatch[0];

            const isValid = (
                targetImage.isConnected &&
                managedElements.has(targetImage) &&
                targetImage.offsetParent !== null &&
                targetImage.naturalHeight > 0 &&
                isFromCurrentChapter
            );

            if (!isValid) {
                targetImage = null;
                activeImageForExport = null;
            }
        }

        const btn = settings.mobileEditMerging ? UI.fabAnki : UI.fabMain;

        if (targetImage) {
            try {
                btn.textContent = '⏳';
                btn.disabled = true;
                const success = await exportImageToAnki(targetImage);
                btn.textContent = success ? '✓' : '✖';
                btn.style.backgroundColor = success ? '#27ae60' : '#c0392b';
            } catch (error) {
                btn.textContent = '❌';
                btn.style.backgroundColor = '#c0392b';
            } finally {
                setTimeout(() => {
                    btn.textContent = settings.mobileEditMerging ? '⛏️' : '➕';
                    btn.style.backgroundColor = '';
                    btn.disabled = false;
                }, 2000);
            }
        } else {
            alert("No images available for export. Please wait for images to load.");
        }
    };

    function bindUIEvents() {
        Object.assign(UI, {
            settingsButton: document.getElementById('gemini-ocr-settings-button'),
            pauseButton: document.getElementById('gemini-ocr-pause-button'),
            settingsModal: document.getElementById('gemini-ocr-settings-modal'),

            fabContainer: document.getElementById('gemini-ocr-fab-container'),
            fabMain: document.getElementById('gemini-ocr-fab-main'),
            fabDelete: document.getElementById('gemini-ocr-fab-delete'),
            fabEdit: document.getElementById('gemini-ocr-fab-edit'),
            fabMerge: document.getElementById('gemini-ocr-fab-merge'),
            fabAnki: document.getElementById('gemini-ocr-fab-anki'),

            debugModal: document.getElementById('gemini-ocr-debug-modal'),
            serverUrlInput: document.getElementById('gemini-ocr-server-url'),
            imageServerUserInput: document.getElementById('gemini-image-server-user'),
            imageServerPasswordInput: document.getElementById('gemini-image-server-password'),
            ankiUrlInput: document.getElementById('gemini-ocr-anki-url'),
            ankiFieldInput: document.getElementById('gemini-ocr-anki-field'),
            debugModeCheckbox: document.getElementById('gemini-ocr-debug-mode'),
            soloHoverCheckbox: document.getElementById('gemini-ocr-solo-hover-mode'),
            addSpaceOnMergeCheckbox: document.getElementById('gemini-ocr-add-space-on-merge'),
            mobileModeCheckbox: document.getElementById('gemini-ocr-mobile-mode'),
            mobileEditMergingCheckbox: document.getElementById('gemini-ocr-mobile-edit-merging'),

            interactionModeSelect: document.getElementById('ocr-interaction-mode'),
            dimmedOpacityInput: document.getElementById('ocr-dimmed-opacity'),
            textOrientationSelect: document.getElementById('ocr-text-orientation'),
            colorThemeSelect: document.getElementById('ocr-color-theme'),
            brightnessModeSelect: document.getElementById('ocr-brightness-mode'),
            focusFontColorSelect: document.getElementById('ocr-focus-font-color'),
            deleteKeyInput: document.getElementById('ocr-delete-key'),
            mergeKeyInput: document.getElementById('ocr-merge-key'),
            fontMultiplierHorizontalInput: document.getElementById('ocr-font-multiplier-horizontal'),
            fontMultiplierVerticalInput: document.getElementById('ocr-font-multiplier-vertical'),
            boundingBoxAdjustmentInput: document.getElementById('ocr-bounding-box-adjustment-input'),
            focusScaleMultiplierInput: document.getElementById('ocr-focus-scale-multiplier'),
            sitesConfigTextarea: document.getElementById('gemini-ocr-sites-config'),
            statusDiv: document.getElementById('gemini-ocr-server-status'),
            debugLogTextarea: document.getElementById('gemini-ocr-debug-log'),
            saveBtn: document.getElementById('gemini-ocr-save-btn'),
            closeBtn: document.getElementById('gemini-ocr-close-btn'),
            debugBtn: document.getElementById('gemini-ocr-debug-btn'),
            closeDebugBtn: document.getElementById('gemini-ocr-close-debug-btn'),
            batchChapterBtn: document.getElementById('gemini-ocr-batch-chapter-btn'),
            purgeCacheBtn: document.getElementById('gemini-ocr-purge-cache-btn')
        });

        UI.settingsButton.addEventListener('click', () => UI.settingsModal.classList.toggle('is-hidden'));

        UI.pauseButton.addEventListener('click', () => {
            isOcrPaused = !isOcrPaused;
            UI.pauseButton.classList.toggle('is-paused', isOcrPaused);
            UI.pauseButton.textContent = isOcrPaused ? '▶️' : '⏸️';
            UI.pauseButton.title = isOcrPaused ? 'Resume OCR Overlay' : 'Pause/Hide OCR Overlay';

            if (isOcrPaused) {
                hideActiveOverlay();
                for (const [, state] of managedElements.entries()) {
                    if (state.overlay) {
                        state.overlay.style.display = 'none';
                    }
                }
                logDebug("OCR Overlay PAUSED.");
            } else {
                for (const [, state] of managedElements.entries()) {
                    if (state.overlay) {
                        state.overlay.style.display = '';
                    }
                }
                logDebug("OCR Overlay RESUMED.");
            }
        });

        // FAB / Button Logic based on Settings
        if (settings.mobileEditMerging) {
            UI.fabMain.addEventListener('click', () => {
                toolState.isMenuOpen = !toolState.isMenuOpen;
                UI.fabContainer.classList.toggle('is-open', toolState.isMenuOpen);
            });

            UI.fabDelete.addEventListener('click', () => {
                toolState.isDeleteMode = !toolState.isDeleteMode;
                if (toolState.isDeleteMode) {
                    toolState.isMergeMode = false;
                    toolState.isEditMode = false;
                    UI.fabMerge.classList.remove('active-mode');
                    UI.fabEdit.classList.remove('active-mode');
                }
                UI.fabDelete.classList.toggle('active-mode', toolState.isDeleteMode);

                toolState.isMenuOpen = false;
                UI.fabContainer.classList.remove('is-open');
            });

            UI.fabEdit.addEventListener('click', () => {
                toolState.isEditMode = !toolState.isEditMode;
                if (toolState.isEditMode) {
                    toolState.isMergeMode = false;
                    toolState.isDeleteMode = false;
                    UI.fabMerge.classList.remove('active-mode');
                    UI.fabDelete.classList.remove('active-mode');
                }
                UI.fabEdit.classList.toggle('active-mode', toolState.isEditMode);

                toolState.isMenuOpen = false;
                UI.fabContainer.classList.remove('is-open');
            });

            UI.fabMerge.addEventListener('click', () => {
                toolState.isMergeMode = !toolState.isMergeMode;
                if (toolState.isMergeMode) {
                    toolState.isEditMode = false;
                    toolState.isDeleteMode = false;
                    UI.fabEdit.classList.remove('active-mode');
                    UI.fabDelete.classList.remove('active-mode');
                } else {
                    if (mergeState.anchorBox) {
                        mergeState.anchorBox.classList.remove('selected-for-merge');
                        mergeState.anchorBox = null;
                    }
                }
                UI.fabMerge.classList.toggle('active-mode', toolState.isMergeMode);

                toolState.isMenuOpen = false;
                UI.fabContainer.classList.remove('is-open');
            });

            UI.fabAnki.addEventListener('click', () => {
                 handleAnkiExportClick();
                 toolState.isMenuOpen = false;
                 UI.fabContainer.classList.remove('is-open');
            });

        } else {
            UI.fabDelete.style.display = 'none';
            UI.fabEdit.style.display = 'none';
            UI.fabMerge.style.display = 'none';
            UI.fabAnki.style.display = 'none';
            UI.fabMain.addEventListener('click', handleAnkiExportClick);
        }

        const relevantAnkiBtn = settings.mobileEditMerging ? UI.fabAnki : UI.fabMain;
        relevantAnkiBtn.addEventListener('mouseenter', () => {
            const state = activeImageForExport ? managedElements.get(activeImageForExport) : null;
            if (state && state.hideTimer) {
                clearTimeout(state.hideTimer);
                state.hideTimer = null;
            }
        });
        relevantAnkiBtn.addEventListener('mouseleave', () => {
            const state = activeImageForExport ? managedElements.get(activeImageForExport) : null;
            if (state) {
                state.hideTimer = setTimeout(() => {
                    if (activeOverlay === state.overlay && !state.overlay.querySelector('.selected-for-merge')) {
                        hideActiveOverlay();
                    }
                    state.hideTimer = null;
                }, 300);
            }
        });

        UI.statusDiv.addEventListener('click', checkServerStatus);
        UI.closeBtn.addEventListener('click', () => UI.settingsModal.classList.add('is-hidden'));
        UI.debugBtn.addEventListener('click', () => {
            UI.debugLogTextarea.value = debugLog.join('\n');
            UI.debugModal.classList.remove('is-hidden');
            UI.debugLogTextarea.scrollTop = UI.debugLogTextarea.scrollHeight;
        });
        UI.closeDebugBtn.addEventListener('click', () => UI.debugModal.classList.add('is-hidden'));
        if (UI.batchChapterBtn) UI.batchChapterBtn.addEventListener('click', window.batchProcessCurrentChapterFromURL || (() => {}));
        UI.purgeCacheBtn.addEventListener('click', purgeServerCache);

        UI.saveBtn.addEventListener('click', async () => {
            const newSettings = {
                ocrServerUrl: UI.serverUrlInput.value.trim(),
                imageServerUser: UI.imageServerUserInput.value.trim(),
                imageServerPassword: UI.imageServerPasswordInput.value,
                ankiConnectUrl: UI.ankiUrlInput.value.trim(),
                ankiImageField: UI.ankiFieldInput.value.trim(),
                debugMode: UI.debugModeCheckbox.checked,
                soloHoverMode: UI.soloHoverCheckbox.checked,
                addSpaceOnMerge: UI.addSpaceOnMergeCheckbox.checked,
                mobileMode: UI.mobileModeCheckbox.checked,
                mobileEditMerging: UI.mobileEditMergingCheckbox.checked,

                interactionMode: UI.interactionModeSelect.value,
                textOrientation: UI.textOrientationSelect.value,
                colorTheme: UI.colorThemeSelect.value,
                brightnessMode: UI.brightnessModeSelect.value,
                deleteModifierKey: UI.deleteKeyInput.value.trim(),
                mergeModifierKey: UI.mergeKeyInput.value.trim(),
                dimmedOpacity: (parseInt(UI.dimmedOpacityInput.value, 10) || 30) / 100,
                fontMultiplierHorizontal: parseFloat(UI.fontMultiplierHorizontalInput.value) || 1.0,
                fontMultiplierVertical: parseFloat(UI.fontMultiplierVerticalInput.value) || 1.0,
                boundingBoxAdjustment: parseInt(UI.boundingBoxAdjustmentInput.value, 10) || 0,
                focusScaleMultiplier: parseFloat(UI.focusScaleMultiplierInput.value) || 1.1,
                focusFontColor: UI.focusFontColorSelect.value,
                sites: UI.sitesConfigTextarea.value.split('\n').filter(line => line.trim()).map(line => {
                    const parts = line.split(';').map(s => s.trim());
                    return {
                        urlPattern: parts[0] || '',
                        overflowFixSelector: parts[1] || '',
                        imageContainerSelectors: parts.slice(2, -1).filter(s => s),
                        contentRootSelector: parts[parts.length - 1] || '#root'
                    };
                })
            };
            try {
                await GM_setValue(SETTINGS_KEY, JSON.stringify(newSettings));
                alert('Settings Saved. The page will now reload.');
                window.location.reload();
            } catch (e) {
                logDebug(`Failed to save settings: ${e.message}`);
                alert(`Error: Could not save settings.`);
            }
        });

        document.addEventListener('ocr-log-update', () => {
            if (UI.debugModal && !UI.debugModal.classList.contains('is-hidden')) {
                UI.debugLogTextarea.value = debugLog.join('\n');
                UI.debugLogTextarea.scrollTop = UI.debugLogTextarea.scrollHeight;
            }
        });
    }

    function getValidImagesForExport() {
        const currentChapterMatch = window.location.pathname.match(/\/manga\/\d+\/chapter\/\d+/);
        const validImages =[];
        const existingImageElements = new Set();

        const checkAndAdd = (img, forceViewportCheck) => {
            if (existingImageElements.has(img)) return;
            const imageChapterMatch = img.src.match(/\/manga\/\d+\/chapter\/\d+/);
            const isFromCurrentChapter = currentChapterMatch && imageChapterMatch &&
                                         currentChapterMatch[0] === imageChapterMatch[0];

            if (img.isConnected && managedElements.has(img) && img.naturalHeight > 0 && isFromCurrentChapter) {
                const rect = img.getBoundingClientRect();
                const isInViewport = rect.top < window.innerHeight && rect.bottom > 0;

                if (!forceViewportCheck || isInViewport) {
                    validImages.push({
                        image: img,
                        rect: rect,
                        isInViewport: isInViewport,
                        pageNumber: img.src.match(/page\/(\d+)/) ? parseInt(img.src.match(/page\/(\d+)/)[1]) : -1
                    });
                    existingImageElements.add(img);
                }
            }
        };

        for (const img of recentlyHoveredImages) {
            checkAndAdd(img, false);
        }

        if (validImages.length < 2) {
            for (const img of visibleImages) {
                checkAndAdd(img, true);
            }
        }
        validImages.sort((a, b) => a.pageNumber - b.pageNumber);
        return validImages;
    }

    function checkServerStatus() {
        const serverUrl = UI.serverUrlInput.value.trim(); if (!serverUrl) return;
        UI.statusDiv.className = 'status-checking'; UI.statusDiv.textContent = 'Checking...';
        GM_xmlhttpRequest({
            method: 'GET', url: serverUrl, timeout: 5000,
            onload: (res) => {
                try {
                    const data = JSON.parse(res.responseText);
                    if (data.status === 'running') {
                        UI.statusDiv.className = 'status-ok';
                        const jobs = data.active_preprocess_jobs ?? 'N/A';
                        UI.statusDiv.textContent = `Connected (Cache: ${data.items_in_cache} | Active Jobs: ${jobs})`;
                    } else {
                        throw new Error('Unresponsive');
                    }
                } catch (e) {
                    UI.statusDiv.className = 'status-error';
                    UI.statusDiv.textContent = 'Invalid Response';
                }
            },
            onerror: () => { UI.statusDiv.className = 'status-error'; UI.statusDiv.textContent = 'Connection Failed'; },
            ontimeout: () => { UI.statusDiv.className = 'status-error'; UI.statusDiv.textContent = 'Timed Out'; }
        });
    }

    function purgeServerCache() {
        if (!confirm("Are you sure you want to permanently delete all items from the server's OCR cache file?")) return;
        const btn = UI.purgeCacheBtn; const originalText = btn.textContent; btn.disabled = true; btn.textContent = 'Purging...';
        GM_xmlhttpRequest({
            method: 'POST',
            url: `${settings.ocrServerUrl}/purge-cache`,
            timeout: 10000,
            onload: (res) => {
                try {
                    const data = JSON.parse(res.responseText);
                    alert(data.message || data.error);
                    checkServerStatus();
                } catch (e) {
                    alert('Failed to parse server response.');
                }
            },
            onerror: () => alert('Failed to connect to server to purge cache.'),
            ontimeout: () => alert('Request to purge cache timed out.'),
            onloadend: () => { btn.disabled = false; btn.textContent = originalText; }
        });
    }

    function createMeasurementSpan() {
        if (measurementSpan) return;
        measurementSpan = document.createElement('span');
        measurementSpan.style.cssText = `position:fixed!important;visibility:hidden!important;height:auto!important;width:auto!important;white-space:normal!important;z-index:-1!important;top:-9999px;left:-9999px;padding:0!important;border:0!important;margin:0!important;`;
        document.body.appendChild(measurementSpan);
    }

    async function init() {
        const loadedSettings = await GM_getValue(SETTINGS_KEY);
        if (loadedSettings) {
            try { settings = { ...settings, ...JSON.parse(loadedSettings) }; }
            catch (e) { logDebug("Could not parse saved settings. Using defaults."); }
        }
        createUI();
        bindUIEvents();
        applyTheme();
        createMeasurementSpan();
        logDebug("Initializing HYBRID engine. Mobile FAB Toggle and Confirmation Logic Added.");

        resizeObserver = new ResizeObserver(handleResize);
        intersectionObserver = new IntersectionObserver(handleIntersection, { rootMargin: '100px 0px' });
        setupMutationObservers();

        UI.serverUrlInput.value = settings.ocrServerUrl;
        UI.imageServerUserInput.value = settings.imageServerUser || '';
        UI.imageServerPasswordInput.value = settings.imageServerPassword || '';
        UI.ankiUrlInput.value = settings.ankiConnectUrl;
        UI.ankiFieldInput.value = settings.ankiImageField;
        UI.debugModeCheckbox.checked = settings.debugMode;
        UI.soloHoverCheckbox.checked = settings.soloHoverMode;
        UI.addSpaceOnMergeCheckbox.checked = settings.addSpaceOnMerge;
        UI.interactionModeSelect.value = settings.interactionMode;
        UI.textOrientationSelect.value = settings.textOrientation;
        UI.colorThemeSelect.value = settings.colorTheme;
        UI.brightnessModeSelect.value = settings.brightnessMode;
        UI.focusFontColorSelect.value = settings.focusFontColor;
        UI.deleteKeyInput.value = settings.deleteModifierKey;
        UI.mergeKeyInput.value = settings.mergeModifierKey;
        UI.dimmedOpacityInput.value = settings.dimmedOpacity * 100;
        UI.fontMultiplierHorizontalInput.value = settings.fontMultiplierHorizontal;
        UI.fontMultiplierVerticalInput.value = settings.fontMultiplierVertical;
        UI.boundingBoxAdjustmentInput.value = settings.boundingBoxAdjustment;
        UI.focusScaleMultiplierInput.value = settings.focusScaleMultiplier;
        UI.sitesConfigTextarea.value = settings.sites.map(s =>[s.urlPattern, s.overflowFixSelector, ...(s.imageContainerSelectors || []), s.contentRootSelector].join('; ')).join('\n');

        UI.mobileModeCheckbox.checked = settings.mobileMode;
        UI.mobileEditMergingCheckbox.checked = settings.mobileEditMerging;
        document.body.classList.toggle('mobile-mode', settings.mobileMode);

        reinitializeScript();
        setupNavigationObserver();
        setupPageChangeDetection();
    }

    init().catch(e => console.error(`[OCR Hybrid] Fatal Initialization Error: ${e.message}`));
})();
