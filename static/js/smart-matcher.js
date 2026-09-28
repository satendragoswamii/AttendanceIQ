/**
 * Smart Matcher Utility Functions
 * Reusable functions for file pairing functionality
 */

function initializeSmartMatcher(formId, addPairBtnId, containerId, pairCountId, isSplitView = false) {
    const matchingForm = document.getElementById(formId);
    const addPairBtn = document.getElementById(addPairBtnId);
    const filePairsContainer = document.getElementById(containerId);

    if (!matchingForm || !addPairBtn || !filePairsContainer) {
        return;
    }
    
    const isLoggedIn = document.body.dataset.userId !== '';
    if (!isLoggedIn) {
        matchingForm.setAttribute('novalidate', 'novalidate');
    }
    const AUTO_BATCH_SCORE_THRESHOLD = 90;

    const modeFileRadio = document.getElementById('match_mode_file');
    const modeBatchRadio = document.getElementById('match_mode_batch');
    const batchSelectWrapper = document.querySelector('.batch-select-wrapper');
    const batchSelect = document.getElementById('batch_id');

    function getAvailableBatches() {
        if (Array.isArray(window.smartMatcherBatches)) {
            return window.smartMatcherBatches;
        }
        if (typeof batchesData !== 'undefined' && Array.isArray(batchesData)) {
            return batchesData;
        }
        return [];
    }

    function stripFileExtension(fileName) {
        return String(fileName || '').replace(/\.[^.\\/]+$/, '');
    }

    function normalizeMatcherText(value) {
        const collapsed = String(value || '')
            .toLowerCase()
            .normalize('NFKD')
            .replace(/[\u0300-\u036f]/g, '')
            .replace(/[_\-./()[\]{}]+/g, ' ')
            .replace(/[^a-z0-9]+/g, ' ')
            .replace(/\b(zoom|attendance|attendancify|report|raw|file|batch|session|sessions|meeting|export|download|csv|xlsx|xls)\b/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
        if (!collapsed) return '';
        // "08" and "8" must count as the same batch number -- filenames rarely
        // zero-pad to match a batch's listed name exactly (e.g. "EPDT 08"
        // should still match "EPDT_Batch_8_Attendance_Report_...").
        return collapsed.split(' ').map(token => /^\d+$/.test(token) ? String(parseInt(token, 10)) : token).join(' ');
    }

    function scoreBatchNameForFile(batchName, fileName) {
        const batchNorm = normalizeMatcherText(batchName);
        const fileNorm = normalizeMatcherText(stripFileExtension(fileName));
        if (!batchNorm || !fileNorm) return 0;

        const batchCompact = batchNorm.replace(/\s+/g, '');
        const fileCompact = fileNorm.replace(/\s+/g, '');
        if (batchCompact.length >= 3 && fileCompact.includes(batchCompact)) {
            return 100 + Math.min(batchCompact.length, 30) / 100;
        }
        if (batchNorm.length >= 3 && (` ${fileNorm} `).includes(` ${batchNorm} `)) {
            return 99 + Math.min(batchNorm.length, 30) / 100;
        }

        // Deliberately no "every token present somewhere" fallback here -- it
        // used to let an unrelated number elsewhere in the filename (a date, a
        // session count, a timestamp) coincidentally satisfy a *different*
        // batch's numeric suffix and get auto-selected instead (e.g. an
        // "EPOSCM 07" file getting matched to "EPOSCM 09"). A batch's name has
        // to appear together, in order, in the filename to count as a match.
        return 0;
    }

    function findBatchMatchForFile(fileName) {
        const batches = getAvailableBatches();
        let best = null;
        let secondBestScore = 0;

        batches.forEach(batch => {
            const score = scoreBatchNameForFile(batch.name, fileName);
            if (!best || score > best.score) {
                secondBestScore = best ? best.score : 0;
                best = { batch, score };
            } else if (score > secondBestScore) {
                secondBestScore = score;
            }
        });

        if (!best || best.score < AUTO_BATCH_SCORE_THRESHOLD) {
            return null;
        }
        if (secondBestScore >= AUTO_BATCH_SCORE_THRESHOLD && best.score - secondBestScore < 1) {
            return null;
        }
        return best;
    }

    function getPairIndex(row, fallbackInput) {
        if (row && row.dataset && row.dataset.pairIndex !== undefined) {
            return row.dataset.pairIndex;
        }
        const name = fallbackInput ? fallbackInput.getAttribute('name') || '' : '';
        const match = name.match(/_(\d+)$/);
        return match ? match[1] : '0';
    }

    function getBatchMatchDisplay(row, batchSelect) {
        if (!row || !batchSelect) return null;
        let display = row.querySelector('.auto-batch-match');
        if (!display) {
            display = document.createElement('div');
            display.className = 'auto-batch-match small mt-1';
            display.style.fontSize = '0.62rem';
            batchSelect.insertAdjacentElement('afterend', display);
        }
        return display;
    }

    function setAutoBatchMessage(row, batchSelect, message, type) {
        const display = getBatchMatchDisplay(row, batchSelect);
        if (!display) return;
        display.textContent = message || '';
        display.classList.remove('text-success', 'text-warning', 'text-muted');
        if (type === 'success') {
            display.classList.add('text-success');
        } else if (type === 'warning') {
            display.classList.add('text-warning');
        } else {
            display.classList.add('text-muted');
        }
    }

    function markBatchSelectManual(batchSelect) {
        if (!batchSelect || batchSelect.dataset.smartMatcherBatchListener === '1') return;
        batchSelect.dataset.smartMatcherBatchListener = '1';
        batchSelect.addEventListener('change', function() {
            if (this.dataset.suppressManualMark === '1') return;
            this.dataset.userSelected = this.value ? '1' : '';
            this.dataset.autoMatched = '';
            const row = this.closest('.file-pair-row');
            setAutoBatchMessage(row, this, '', 'muted');
        });
    }

    function selectBatchHierarchically(pairIndex, batchId) {
        const subgroupSelect = document.getElementById('subgroup_id_' + pairIndex);
        const batchSelect = document.getElementById('batch_id_' + pairIndex);

        if (!batchSelect) return false;

        const batches = getAvailableBatches();
        const batch = batches.find(b => b.id == batchId);
        if (!batch) return false;

        // Populate this pair's own Sub-Group/Batch options directly from the
        // matched batch's group/subgroup, without touching the shared global
        // Group selector. Changing that dropdown fires a "change" handler that
        // resets *every* pair's Sub-Group/Batch selection back to empty -- so
        // during bulk/multi-pair auto-detection, as soon as two pairs' correct
        // batches belonged to different groups (or one was ungrouped and
        // another wasn't), each pair's selection wiped out the ones before it.
        const subgroups = window.smartMatcherSubgroups || [];
        if (subgroupSelect) {
            if (batch.groupId) {
                const filteredSubgroups = subgroups.filter(sg => sg.groupId == batch.groupId);
                let sgHtml = '<option value="">Sub-Group...</option>';
                filteredSubgroups.forEach(sg => { sgHtml += `<option value="${sg.id}">${sg.name}</option>`; });
                subgroupSelect.innerHTML = sgHtml;
                subgroupSelect.disabled = false;
                subgroupSelect.value = batch.subgroupId || '';
            } else {
                subgroupSelect.innerHTML = '<option value="">Sub-Group...</option>';
                subgroupSelect.disabled = true;
                subgroupSelect.value = '';
            }
        }

        const filteredBatches = batch.subgroupId
            ? batches.filter(b => b.subgroupId == batch.subgroupId)
            : batches.filter(b => b.groupId == batch.groupId && !b.subgroupId);
        let batchHtml = '<option value="">Batch...</option>';
        filteredBatches.forEach(b => { batchHtml += `<option value="${b.id}">${b.name} (${b.count} students)</option>`; });
        batchSelect.innerHTML = batchHtml;
        batchSelect.disabled = false;
        batchSelect.value = batchId;
        batchSelect.dispatchEvent(new Event('change'));
        return true;
    }

    function setupHierarchicalSelectors(wrapper, pairIndex) {
        if (!wrapper) return;
        
        wrapper.innerHTML = `
            <div class="row g-1">
                <div class="col-6">
                    <select class="form-select form-select-sm smart-subgroup-select" id="subgroup_id_${pairIndex}" style="font-size: 0.65rem; padding: 2px 4px; height: 26px;" disabled>
                        <option value="">Sub-Group...</option>
                    </select>
                </div>
                <div class="col-6">
                    <select class="form-select form-select-sm smart-batch-select" name="batch_id_${pairIndex}" id="batch_id_${pairIndex}" style="font-size: 0.65rem; padding: 2px 4px; height: 26px;" disabled>
                        <option value="">Batch...</option>
                    </select>
                </div>
            </div>
        `;

        const subgroupSelect = wrapper.querySelector('.smart-subgroup-select');
        const batchSelect = wrapper.querySelector('.smart-batch-select');

        const globalGroupSelect = document.getElementById('global_group_id');
        const groupId = globalGroupSelect ? globalGroupSelect.value : '';

        const subgroups = window.smartMatcherSubgroups || [];
        const batches = getAvailableBatches();

        if (groupId) {
            const filteredSubgroups = subgroups.filter(sg => sg.groupId == groupId);
            let sgHtml = '<option value="">Sub-Group...</option>';
            filteredSubgroups.forEach(sg => {
                sgHtml += `<option value="${sg.id}">${sg.name}</option>`;
            });
            subgroupSelect.innerHTML = sgHtml;
            subgroupSelect.disabled = false;

            // No sub-group selected yet: show only the group's batches that aren't in any sub-group
            const filteredBatches = batches.filter(b => b.groupId == groupId && !b.subgroupId);
            let bHtml = '<option value="">Batch...</option>';
            filteredBatches.forEach(b => {
                bHtml += `<option value="${b.id}">${b.name} (${b.count} students)</option>`;
            });
            batchSelect.innerHTML = bHtml;
            batchSelect.disabled = false;
        }

        subgroupSelect.onchange = function() {
            const subgroupId = this.value;
            const currentGroupId = globalGroupSelect ? globalGroupSelect.value : '';

            if (subgroupId) {
                // Sub-group selected: narrow down to just that sub-group's batches
                const filteredBatches = batches.filter(b => b.subgroupId == subgroupId);
                let bHtml = '<option value="">Batch...</option>';
                filteredBatches.forEach(b => {
                    bHtml += `<option value="${b.id}">${b.name} (${b.count} students)</option>`;
                });
                batchSelect.innerHTML = bHtml;
                batchSelect.disabled = false;
            } else if (currentGroupId) {
                // No sub-group selected: fall back to the group's batches that aren't in any sub-group
                const filteredBatches = batches.filter(b => b.groupId == currentGroupId && !b.subgroupId);
                let bHtml = '<option value="">Batch...</option>';
                filteredBatches.forEach(b => {
                    bHtml += `<option value="${b.id}">${b.name} (${b.count} students)</option>`;
                });
                batchSelect.innerHTML = bHtml;
                batchSelect.disabled = false;
            } else {
                batchSelect.innerHTML = '<option value="">Batch...</option>';
                batchSelect.disabled = true;
            }

            updatePairStatus(pairIndex);
        };
    }

    function setBatchSelectValue(batchSelect, value, isAuto) {
        batchSelect.dataset.suppressManualMark = '1';
        batchSelect.value = value || '';
        if (isAuto) {
            batchSelect.dataset.autoMatched = value ? '1' : '';
            batchSelect.dataset.userSelected = '';
        }
        batchSelect.dataset.suppressManualMark = '';
    }

    function autoSelectBatchForRawInput(rawInput) {
        if (!rawInput || !rawInput.files || rawInput.files.length === 0) return;
        const row = rawInput.closest('.file-pair-row');
        if (!row) return;

        const pairIndex = getPairIndex(row, rawInput);
        const sourceSelect = row.querySelector('.master-source-select');
        const batchSelect = row.querySelector('[name="batch_id_' + pairIndex + '"]');
        if (!batchSelect) return;

        markBatchSelectManual(batchSelect);
        if (batchSelect.dataset.userSelected === '1') return;

        const fileName = rawInput.files[0].name;
        const match = findBatchMatchForFile(fileName);
        if (match) {
            if (sourceSelect && sourceSelect.value !== 'batch') {
                sourceSelect.value = 'batch';
                if (typeof toggleMasterInput === 'function') {
                    toggleMasterInput(pairIndex, 'batch');
                }
            }
            batchSelect.dataset.autoMatched = String(match.batch.id);
            selectBatchHierarchically(pairIndex, String(match.batch.id));
            setAutoBatchMessage(row, batchSelect, 'Auto-selected: ' + match.batch.name, 'success');
        } else if (batchSelect.dataset.autoMatched === '1' || !batchSelect.value) {
            const groupSelect = document.getElementById('group_id_' + pairIndex);
            if (groupSelect) {
                groupSelect.value = '';
                groupSelect.dispatchEvent(new Event('change'));
            }
            setAutoBatchMessage(row, batchSelect, 'No clear batch match', 'warning');
        }

        if (typeof updatePairStatus === 'function') {
            updatePairStatus(pairIndex);
        }
    }

    function assignSingleFileToInput(inputElement, file) {
        try {
            const transfer = new DataTransfer();
            transfer.items.add(file);
            inputElement.files = transfer.files;
            return true;
        } catch (error) {
            return false;
        }
    }

    function handleRawInputChange(inputElement) {
        const row = inputElement.closest('.file-pair-row');
        const pairIndex = getPairIndex(row, inputElement);
        updateFileNameDisplay(inputElement, 'raw_file_name_' + pairIndex);

        const selectedFiles = Array.from(inputElement.files || []);
        if (inputElement.dataset.skipBulkSplit === '1') {
            inputElement.dataset.skipBulkSplit = '';
            autoSelectBatchForRawInput(inputElement);
            return;
        }

        if (selectedFiles.length > 1) {
            const firstFile = selectedFiles[0];
            inputElement.dataset.skipBulkSplit = '1';
            if (assignSingleFileToInput(inputElement, firstFile)) {
                inputElement.dispatchEvent(new Event('change', { bubbles: true }));
            }

            // Bulk upload: a raw file per pair is created automatically. In file
            // mode the same master roster almost always applies to every raw
            // file in the batch, so clone it into each new pair instead of
            // forcing a re-upload per pair. Batch mode already auto-detects the
            // best-fit batch per file, so it's left alone.
            const sourceSelect = row ? row.querySelector('.master-source-select') : null;
            const usingFileMode = !sourceSelect || sourceSelect.value === 'file';
            const masterInput = row ? row.querySelector('.master-input') : null;
            const masterFile = (usingFileMode && masterInput && masterInput.files && masterInput.files[0]) || null;

            selectedFiles.slice(1).forEach(file => {
                addPairBtn.click();
                const rows = filePairsContainer.querySelectorAll('.file-pair-row');
                const newRow = rows[rows.length - 1];
                const newRawInput = newRow ? (newRow.querySelector('.raw-input') || newRow.querySelector('[name^="raw_file_"]')) : null;
                if (newRawInput && assignSingleFileToInput(newRawInput, file)) {
                    newRawInput.dataset.skipBulkSplit = '1';
                    newRawInput.dispatchEvent(new Event('change', { bubbles: true }));
                }
                if (masterFile && newRow) {
                    const newSourceSelect = newRow.querySelector('.master-source-select');
                    const newPairIndex = getPairIndex(newRow, newRawInput);
                    if (newSourceSelect) {
                        newSourceSelect.value = 'file';
                        newSourceSelect.dispatchEvent(new Event('change'));
                    }
                    const newMasterInput = newRow.querySelector('.master-input');
                    if (newMasterInput && assignSingleFileToInput(newMasterInput, masterFile)) {
                        updateFileNameDisplay(newMasterInput, 'master_file_name_' + newPairIndex);
                        if (typeof updatePairStatus === 'function') {
                            updatePairStatus(newPairIndex);
                        }
                    }
                }
            });
            return;
        }

        autoSelectBatchForRawInput(inputElement);
    }

    // Function to update file name display
    function updateFileNameDisplay(inputElement, displayElementId) {
        const fileName = inputElement.files[0] ? inputElement.files[0].name : '';
        const displayElement = document.getElementById(displayElementId);
        if (displayElement) {
            displayElement.textContent = fileName ? `Selected: ${fileName}` : '';
        }
    }

    function updateModeUI() {
        const usingBatch = modeBatchRadio && modeBatchRadio.checked;
        // Handle both class names for compatibility (standalone page uses master-mode-wrapper, split view uses master-file-wrapper)
        const masterWrappers = filePairsContainer.querySelectorAll('.master-mode-wrapper, .master-file-wrapper');
        const masterInputs = filePairsContainer.querySelectorAll('.master-input');

        // A pair with its own per-pair "Upload File"/"Use Batch" selector
        // manages its own master-file-wrapper visibility and required-ness
        // (see toggleMasterInput / autoSelectBatchForRawInput) independently
        // per file. The global Match Mode radio must not override that --
        // otherwise a per-pair auto-detected batch selection gets its master
        // file input silently forced back to required/visible.
        const hasOwnSourceSelect = el => Boolean(el.closest('.file-pair-row')?.querySelector('.master-source-select'));

        masterWrappers.forEach(w => {
            if (hasOwnSourceSelect(w)) return;
            if (usingBatch) {
                w.classList.add('d-none');
            } else {
                w.classList.remove('d-none');
            }
        });
        masterInputs.forEach(inp => {
            if (hasOwnSourceSelect(inp)) return;
            if (usingBatch) {
                inp.removeAttribute('required');
            } else {
                inp.setAttribute('required', 'required');
            }
        });

        const globalSubgroupCol = document.getElementById('global_subgroup_col');
        const globalBatchCol = document.getElementById('global_batch_col');
        const matchModeDesc = document.getElementById('match_mode_description');

        if (usingBatch) {
            if (globalSubgroupCol) globalSubgroupCol.style.display = 'block';
            if (globalBatchCol) globalBatchCol.style.display = 'block';
            if (matchModeDesc) matchModeDesc.textContent = 'Batch mode: one selected batch is matched against each uploaded raw file.';
        } else {
            if (globalSubgroupCol) globalSubgroupCol.style.display = 'none';
            if (globalBatchCol) globalBatchCol.style.display = 'none';
            if (matchModeDesc) matchModeDesc.textContent = 'File mode: match multiple file pairs, either upload master file or select batch for each pair.';
        }
    }

    function updateAllPairsHierarchicalSelectors() {
        const globalGroupSelect = document.getElementById('global_group_id');
        const groupId = globalGroupSelect ? globalGroupSelect.value : '';

        const subgroups = window.smartMatcherSubgroups || [];
        const batches = getAvailableBatches();

        const pairRows = filePairsContainer.querySelectorAll('.file-pair-row');
        pairRows.forEach(row => {
            const pairIndex = getPairIndex(row);
            const subgroupSelect = row.querySelector('.smart-subgroup-select');
            const batchSelect = row.querySelector('.smart-batch-select');

            if (!subgroupSelect || !batchSelect) return;

            subgroupSelect.innerHTML = '<option value="">Sub-Group...</option>';
            batchSelect.innerHTML = '<option value="">Batch...</option>';

            if (groupId) {
                const filteredSubgroups = subgroups.filter(sg => sg.groupId == groupId);
                let sgHtml = '<option value="">Sub-Group...</option>';
                filteredSubgroups.forEach(sg => {
                    sgHtml += `<option value="${sg.id}">${sg.name}</option>`;
                });
                subgroupSelect.innerHTML = sgHtml;
                subgroupSelect.disabled = false;

                // No sub-group selected yet: show only the group's batches that aren't in any sub-group
                const filteredBatches = batches.filter(b => b.groupId == groupId && !b.subgroupId);
                let bHtml = '<option value="">Batch...</option>';
                filteredBatches.forEach(b => {
                    bHtml += `<option value="${b.id}">${b.name} (${b.count} students)</option>`;
                });
                batchSelect.innerHTML = bHtml;
                batchSelect.disabled = false;
            } else {
                subgroupSelect.disabled = true;
                batchSelect.disabled = true;
            }

            if (typeof updatePairStatus === 'function') {
                updatePairStatus(pairIndex);
            }
        });
    }

    // Add event listeners for batch mode radios
    if (modeFileRadio) {
        modeFileRadio.addEventListener('change', updateModeUI);
    }
    if (modeBatchRadio) {
        modeBatchRadio.addEventListener('change', updateModeUI);
    }
    
    // Initialize mode UI on page load
    updateModeUI();

    function wireBatchSelects() {
        filePairsContainer
            .querySelectorAll('.master-batch-wrapper select, [name^="batch_id_"]')
            .forEach(markBatchSelectManual);
    }
    
    // Setup global Group selection listener
    const globalGroupSelect = document.getElementById('global_group_id');
    if (globalGroupSelect) {
        // Populate global Group dropdown dynamically if it has no options
        if (globalGroupSelect.options.length <= 1) {
            const groups = window.smartMatcherGroups || [];
            let groupHtml = '<option value="">Select Group...</option>';
            groups.forEach(g => {
                groupHtml += `<option value="${g.id}">${g.name}</option>`;
            });
            globalGroupSelect.innerHTML = groupHtml;
        }

        globalGroupSelect.addEventListener('change', function() {
    const groupId = this.value;


    const globalSubgroupSelect = document.getElementById('global_subgroup_id');
    const globalBatchSelect = document.getElementById('batch_id');
    
    if (globalSubgroupSelect && globalBatchSelect) {
        globalSubgroupSelect.innerHTML = '<option value="">Select Sub-Group...</option>';
        globalBatchSelect.innerHTML = '<option value="">Select Batch...</option>';

        if (groupId) {
            const subgroups = window.smartMatcherSubgroups || [];
            const filteredSgs = subgroups.filter(sg => sg.groupId == groupId);
            let sgHtml = '<option value="">Select Sub-Group...</option>';
            filteredSgs.forEach(sg => { sgHtml += `<option value="${sg.id}">${sg.name}</option>`; });
            globalSubgroupSelect.innerHTML = sgHtml;
            globalSubgroupSelect.disabled = false;

            // No sub-group selected yet: show only the group's batches that aren't in any sub-group
            const batches = getAvailableBatches();
            const filteredBs = batches.filter(b => b.groupId == groupId && !b.subgroupId);
            let bHtml = '<option value="">Select Batch...</option>';
            filteredBs.forEach(b => { bHtml += `<option value="${b.id}">${b.name} (${b.count} students)</option>`; });
            globalBatchSelect.innerHTML = bHtml;
            globalBatchSelect.disabled = false;
        } else {
            globalSubgroupSelect.disabled = true;
            globalBatchSelect.disabled = true;
        }
    }
    
    // 2. Update all per-pair selectors
    updateAllPairsHierarchicalSelectors();
    // Reset selected values for each pair
    const pairRows = filePairsContainer.querySelectorAll('.file-pair-row');
    pairRows.forEach(row => {
    const subgroupSelect = row.querySelector('.smart-subgroup-select');
    const batchSelect = row.querySelector('.smart-batch-select');
    if (subgroupSelect) subgroupSelect.value = '';
    if (batchSelect) batchSelect.value = '';
});
        });

        // Exactly one group available: there is nothing to pick, so the
        // dropdown is hidden by the template and pre-selected server-side.
        // Fire the cascade manually since a hidden select never gets a
        // user-driven 'change' event.
        if ((window.smartMatcherGroups || []).length === 1) {
            globalGroupSelect.dispatchEvent(new Event('change'));
        }
    }

    const globalSubgroupSelect = document.getElementById('global_subgroup_id');
    if (globalSubgroupSelect) {
        globalSubgroupSelect.addEventListener('change', function() {
            const subgroupId = this.value;
            const globalBatchSelect = document.getElementById('batch_id');
            if (globalBatchSelect) {
                const batches = getAvailableBatches();
                const groupId = globalGroupSelect ? globalGroupSelect.value : '';

                if (subgroupId) {
                    // Sub-group selected: narrow down to just that sub-group's batches
                    const filteredBs = batches.filter(b => b.subgroupId == subgroupId);
                    let bHtml = '<option value="">Select Batch...</option>';
                    filteredBs.forEach(b => { bHtml += `<option value="${b.id}">${b.name} (${b.count} students)</option>`; });
                    globalBatchSelect.innerHTML = bHtml;
                    globalBatchSelect.disabled = false;
                } else if (groupId) {
                    // No sub-group selected: fall back to the group's batches that aren't in any sub-group
                    const filteredBs = batches.filter(b => b.groupId == groupId && !b.subgroupId);
                    let bHtml = '<option value="">Select Batch...</option>';
                    filteredBs.forEach(b => { bHtml += `<option value="${b.id}">${b.name} (${b.count} students)</option>`; });
                    globalBatchSelect.innerHTML = bHtml;
                    globalBatchSelect.disabled = false;
                } else {
                    globalBatchSelect.innerHTML = '<option value="">Select Batch...</option>';
                    globalBatchSelect.disabled = true;
                }
            }
        });
    }
    
    // Setup hierarchical selectors for the initial row if batch matching is active
    const firstBatchWrapper = filePairsContainer.querySelector('.master-batch-wrapper');
    if (firstBatchWrapper) {
        setupHierarchicalSelectors(firstBatchWrapper, 0);
    }
    
    wireBatchSelects();

    // Add event listeners to initial file inputs

    const initialMasterInput = filePairsContainer.querySelector('input[name="master_file_0"]');
    const initialRawInput = filePairsContainer.querySelector('input[name="raw_file_0"]');
    
    if (initialMasterInput) {
        initialMasterInput.addEventListener('change', function() {
            updateFileNameDisplay(this, 'master_file_name_0');
        });

    }
    
    if (initialRawInput) {
        initialRawInput.addEventListener('change', function() {
            handleRawInputChange(this);
        });
    }
    
    // Form submission behavior

    if (matchingForm) {
        matchingForm.addEventListener('submit', function(e) {
            console.log('[Smart Matcher] Form submit triggered');
            
            if (!isLoggedIn) {
                // Redirect anonymous users to login without processing
                e.preventDefault();
                window.location.href = '/login';
                return;
            }

            // Clear previous error highlighting
            filePairsContainer.querySelectorAll('.file-pair-row').forEach(row => {
                row.classList.remove('border-danger');
                row.classList.remove('border-2');
            });

            // Get all file pair rows
            const allRows = filePairsContainer.querySelectorAll('.file-pair-row');
            let firstErrorMessage = '';
            let hasError = false;
            let pairNum = 0;

            console.log('[Smart Matcher] Validating ' + allRows.length + ' pairs');

            allRows.forEach((row, index) => {
                pairNum++;
                const rawInput = row.querySelector('.raw-input');
                const serverFileInput = row.querySelector('[name^="server_file_id_"]');
                const hasAttachedServerFile = Boolean(serverFileInput && serverFileInput.value);
                const sourceSelect = row.querySelector('.master-source-select');
                const usingGlobalBatch = modeBatchRadio && modeBatchRadio.checked;
                const batchSelect = row.querySelector('.master-batch-wrapper .smart-batch-select') || row.querySelector('.master-batch-wrapper select[name^="batch_id"]') || row.querySelector('.master-batch-wrapper select') || (usingGlobalBatch ? document.getElementById('batch_id') : null);
                const masterInput = row.querySelector('.master-input');
                
                const isPairBatchMode = usingGlobalBatch || (sourceSelect && sourceSelect.value === 'batch');
                
                console.log('[Smart Matcher] Pair ' + pairNum + ': batchMode=' + isPairBatchMode + 
                    ', batchValue=' + (batchSelect ? batchSelect.value : 'N/A') +
                    ', rawFile=' + (rawInput && rawInput.files && rawInput.files.length > 0 ? 'YES' : 'NO'));
                
                // Check raw file - always required
                if (!hasAttachedServerFile && (!rawInput || !rawInput.files || rawInput.files.length === 0)) {
                    hasError = true;
                    if (!firstErrorMessage) {
                        firstErrorMessage = `Pair #${pairNum}: Please select a raw file.`;
                    }
                    row.classList.add('border-danger', 'border-2');
                    return;
                }
                
                if (isPairBatchMode) {
                    // Batch mode: only need batch selection + raw file
                    if (!batchSelect || !batchSelect.value) {
                        hasError = true;
                        if (!firstErrorMessage) {
                            firstErrorMessage = `Pair #${pairNum}: Please select a batch.`;
                        }
                        row.classList.add('border-danger', 'border-2');
                    }
                    // Master file NOT required in batch mode
                } else {
                    // File mode: need master file + raw file
                    if (!masterInput || !masterInput.files || masterInput.files.length === 0) {
                        hasError = true;
                        if (!firstErrorMessage) {
                            firstErrorMessage = `Pair #${pairNum}: Please select a master file.`;
                        }
                        row.classList.add('border-danger', 'border-2');
                    }
                }
            });

            if (hasError) {
                console.log('[Smart Matcher] Validation failed: ' + firstErrorMessage);
                e.preventDefault();
                if (firstErrorMessage) {
                    alert(firstErrorMessage);
                }
            } else {
                console.log('[Smart Matcher] Validation passed, submitting form...');
                // Let the form submit naturally, but show a loading state
                const matchSubmitBtn = matchingForm.querySelector('button[type="submit"]');
                if (matchSubmitBtn) {
                    matchSubmitBtn.dataset.originalHtml = matchSubmitBtn.innerHTML;
                    matchSubmitBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>Matching...';
                    matchSubmitBtn.disabled = true;
                }
            }
        });
    }
    
    // Add pair button functionality
    // Check if batch selection is available (superadmin or batch matching enabled)
    // Wait a tick to ensure the DOM is ready with the batch selector from initializeBatchSelection
    let hasBatchOption = false;
    let batchOptionsHtml = '';
    
    // Function to refresh batch options - called after a short delay to ensure DOM is ready
    function refreshBatchOptions() {
        hasBatchOption = document.querySelector('.master-source-select') !== null;
        const batchSelect = document.querySelector('[name="batch_id_0"]');
        batchOptionsHtml = (hasBatchOption && batchSelect) ? batchSelect.innerHTML : '';
        console.log('[Smart Matcher] Batch options refreshed: hasBatchOption=' + hasBatchOption + ', options=' + (batchOptionsHtml ? 'found' : 'empty'));
    }
    
    // Initial refresh after a short delay
    setTimeout(refreshBatchOptions, 100);

    addPairBtn.addEventListener('click', function() {
        // Refresh batch options before adding pair
        refreshBatchOptions();
        
        const pairCountInput = document.getElementById(pairCountId);
        let pairIndex = parseInt(pairCountInput.value);
        const newPair = document.createElement('div');

        newPair.className = 'file-pair-row card border mb-1';
        newPair.setAttribute('data-pair-index', pairIndex);
        newPair.setAttribute('data-status', 'incomplete');
        newPair.style.borderRadius = '6px';
        
        let masterSourceHtml = '';
        let batchSelectHtml = '';
        let rawSpacerHtml = '';
        
        if (hasBatchOption) {
            masterSourceHtml = `
                <select class="form-select form-select-sm master-source-select mb-1" name="master_source_${pairIndex}" onchange="toggleMasterInput(${pairIndex}, this.value)" style="font-size: 0.65rem; padding: 2px 4px; height: 24px;">
                    <option value="file">Upload File</option>
                    <option value="batch" selected>Use Batch</option>
                </select>
            `;
            batchSelectHtml = `
                <div class="master-batch-wrapper" id="master-batch-wrapper-${pairIndex}" style="display: block;">
                    <div class="row g-1">
                        <div class="col-6">
                            <select class="form-select form-select-sm smart-subgroup-select" id="subgroup_id_${pairIndex}" style="font-size: 0.65rem; padding: 2px 4px; height: 26px;" disabled>
                                <option value="">Sub-Group...</option>
                            </select>
                        </div>
                        <div class="col-6">
                            <select class="form-select form-select-sm smart-batch-select" name="batch_id_${pairIndex}" id="batch_id_${pairIndex}" style="font-size: 0.65rem; padding: 2px 4px; height: 26px;" disabled>
                                <option value="">Batch...</option>
                            </select>
                        </div>
                    </div>
                </div>
            `;
            rawSpacerHtml = '<div style="height: 24px;"></div>';
        }
        
        // If batch option exists, default to batch mode (hide file input completely)
        const fileWrapperStyle = hasBatchOption ? 'display: none;' : 'display: block;';
        const fileWrapperClass = hasBatchOption ? 'master-file-wrapper d-none' : 'master-file-wrapper';
        // Disable the master input if batch mode to prevent browser validation errors
        const masterInputDisabled = hasBatchOption ? 'disabled' : '';
        
        newPair.innerHTML = `
            <div class="card-header pair-header py-1 px-2 d-flex align-items-center justify-content-between" style="cursor: pointer; min-height: 28px;" onclick="togglePairCollapse(this);">
                <div class="d-flex align-items-center gap-1">
                    <span class="badge bg-primary rounded-pill" style="font-size: 0.6rem; padding: 2px 6px;">${pairIndex + 1}</span>
                    <span class="fw-semibold pair-title" style="font-size: 0.7rem;">Pair</span>
                    <span class="pair-status-text text-muted" style="font-size: 0.65rem;"></span>
                </div>
                <div class="d-flex align-items-center gap-1">
                    <span class="pair-status-icon"></span>
                    <button type="button" class="btn btn-sm btn-outline-danger remove-pair-btn py-0 px-1" style="font-size: 0.6rem;">
                        <i class="fas fa-times"></i>
                    </button>
                    <i class="fas fa-chevron-up collapse-icon text-muted" style="font-size: 0.6rem;"></i>
                </div>
            </div>
            <div class="card-body p-1 pair-body">
                <div class="d-flex gap-2">
                    <div style="flex: 1; min-width: 0;">
                        <label class="form-label mb-0" style="font-size: 0.65rem; font-weight: 600;">
                            <i class="fas fa-file-alt text-primary me-1"></i>Master
                        </label>
                        ${masterSourceHtml}
                        <div class="${fileWrapperClass}" id="master-file-wrapper-${pairIndex}" style="${fileWrapperStyle}">
                            <input class="form-control form-control-sm master-input" type="file" name="master_file_${pairIndex}" accept=".csv,.xlsx" style="font-size: 0.65rem; padding: 2px 4px; height: 26px;" ${masterInputDisabled} onchange="updatePairStatus(${pairIndex});">
                        </div>
                        ${batchSelectHtml}
                    </div>
                    <div style="flex: 1; min-width: 0;">
                        <label class="form-label mb-0" style="font-size: 0.65rem; font-weight: 600;">
                            <i class="fas fa-file-import text-success me-1"></i>Raw
                        </label>
                        ${rawSpacerHtml}
                        <input class="form-control form-control-sm raw-input" type="file" name="raw_file_${pairIndex}" accept=".csv,.xlsx" multiple required style="font-size: 0.65rem; padding: 2px 4px; height: 26px;" onchange="updatePairStatus(${pairIndex});">
                    </div>
                </div>
            </div>
        `;
        filePairsContainer.appendChild(newPair);
        if (hasBatchOption) {
            setupHierarchicalSelectors(newPair.querySelector('.master-batch-wrapper'), pairIndex);
        }
        wireBatchSelects();
        // Update pair count to match actual number of rows
        pairCountInput.value = filePairsContainer.querySelectorAll('.file-pair-row').length;

        const newMasterInput = newPair.querySelector(`input[name="master_file_${pairIndex}"]`);
        const newRawInput = newPair.querySelector(`input[name="raw_file_${pairIndex}"]`);
        
        if (newMasterInput) {
            newMasterInput.addEventListener('change', function() {
                updateFileNameDisplay(this, `master_file_name_${pairIndex}`);
            });
        }
        
        if (newRawInput) {
            newRawInput.addEventListener('change', function() {
                handleRawInputChange(this);
            });
        }

        // Apply current mode visibility to new row
        updateModeUI();
        
        // Enable remove button for first pair
        const firstRemoveBtn = filePairsContainer.querySelector('.remove-pair-btn');
        if (firstRemoveBtn) {
            firstRemoveBtn.disabled = false;
        }
    });
    
    // Remove pair functionality
    filePairsContainer.addEventListener('click', function(e) {
        if (e.target.closest('.remove-pair-btn')) {
            const row = e.target.closest('.file-pair-row');
            if (filePairsContainer.children.length > 1) {
                row.remove();
                // Update pair_count to reflect current number of pairs
                document.getElementById(pairCountId).value = filePairsContainer.children.length;
                
                // Update pair numbers for remaining pairs
                const pairs = filePairsContainer.querySelectorAll('.file-pair-row');
                pairs.forEach((pair, index) => {
                    const heading = pair.querySelector('h6');
                    if (heading) {
                        heading.textContent = `File Pair #${index + 1}`;
                    }
                    const badge = pair.querySelector('.badge');
                    if (badge) {
                        badge.textContent = String(index + 1);
                    }
                    pair.setAttribute('data-pair-index', index);
                    
                    // Update input names
                    const masterInput = pair.querySelector('[name^="master_file"]');
                    const subgroupSelect = pair.querySelector('.smart-subgroup-select');
                    const batchSelect = pair.querySelector('.smart-batch-select');
                    const sourceContainer = pair.querySelector('[id^="master-source-selector-"]');
                    const fileWrapper = pair.querySelector('[id^="master-file-wrapper-"]');
                    const batchWrapper = pair.querySelector('[id^="master-batch-wrapper-"]');
                    const rawSpacer = pair.querySelector('[id^="raw-spacer-"]');
                    if (masterInput) masterInput.name = `master_file_${index}`;
                    if (rawInput) {
                        rawInput.name = `raw_file_${index}`;
                        rawInput.setAttribute('onchange', `updatePairStatus(${index});`);
                    }
                    if (sourceSelect) {
                        sourceSelect.name = `master_source_${index}`;
                        sourceSelect.setAttribute('onchange', `toggleMasterInput(${index}, this.value)`);
                    }
                    if (subgroupSelect) {
                        subgroupSelect.id = `subgroup_id_${index}`;
                    }
                    if (batchSelect) {
                        batchSelect.name = `batch_id_${index}`;
                        batchSelect.id = `batch_id_${index}`;
                        batchSelect.setAttribute('onchange', `updatePairStatus(${index});`);
                    }
                    if (sourceContainer) sourceContainer.id = `master-source-selector-${index}`;
                    if (fileWrapper) fileWrapper.id = `master-file-wrapper-${index}`;
                    if (batchWrapper) batchWrapper.id = `master-batch-wrapper-${index}`;
                    if (rawSpacer) rawSpacer.id = `raw-spacer-${index}`;
                    
                    // Update file name display IDs
                    const masterFileNameDisplay = pair.querySelector('[id^="master_file_name"]');
                    const rawFileNameDisplay = pair.querySelector('[id^="raw_file_name"]');
                    if (masterFileNameDisplay) masterFileNameDisplay.id = `master_file_name_${index}`;
                    if (rawFileNameDisplay) rawFileNameDisplay.id = `raw_file_name_${index}`;
                });
                wireBatchSelects();
                
                // Disable remove button for first pair if only one pair left
                if (filePairsContainer.children.length === 1) {
                    const firstRemoveBtn = filePairsContainer.querySelector('.remove-pair-btn');
                    if (firstRemoveBtn) {
                        firstRemoveBtn.disabled = true;
                    }
                }
            }
        }
    });
    
    // Expose hierarchical selector helper functions globally
    window.setupHierarchicalSelectors = setupHierarchicalSelectors;
    window.selectBatchHierarchically = selectBatchHierarchically;
    
    // Allow normal form submission for file downloads when validation passes
}

// Global helper functions for Smart Matcher
function togglePairCollapse(header) {
    const body = header.nextElementSibling;
    const icon = header.querySelector('.collapse-icon');
    if (body) body.classList.toggle('d-none');
    if (icon) {
        icon.classList.toggle('fa-chevron-down');
        icon.classList.toggle('fa-chevron-up');
    }
}

function updatePairStatus(pairIndex) {
    const pairRow = document.querySelector('[data-pair-index="' + pairIndex + '"]');
    if (!pairRow) return;
    
    const header = pairRow.querySelector('.pair-header');
    const statusText = pairRow.querySelector('.pair-status-text');
    const statusIcon = pairRow.querySelector('.pair-status-icon');
    
    // Check master source
    const sourceSelect = pairRow.querySelector('.master-source-select');
    const masterInput = pairRow.querySelector('.master-input');
    const batchSelect = pairRow.querySelector('[name^="batch_id_"]');
    const rawInput = pairRow.querySelector('.raw-input');
    
    let masterName = '';
    let hasMaster = false;
    let hasRaw = rawInput && rawInput.files && rawInput.files.length > 0;
    
    if (sourceSelect && sourceSelect.value === 'batch') {
        // Batch mode
        if (batchSelect && batchSelect.value) {
            hasMaster = true;
            masterName = batchSelect.options[batchSelect.selectedIndex].text.split(' (')[0];
        }
    } else {
        // File mode
        if (masterInput && masterInput.files && masterInput.files.length > 0) {
            hasMaster = true;
            masterName = masterInput.files[0].name.substring(0, 20) + (masterInput.files[0].name.length > 20 ? '...' : '');
        }
    }
    
    let rawName = '';
    if (hasRaw) {
        rawName = rawInput.files[0].name.substring(0, 15) + (rawInput.files[0].name.length > 15 ? '...' : '');
    }
    
    const isBatchMode = sourceSelect && sourceSelect.value === 'batch';

    // Update status
    if (header) {
        if (hasMaster && hasRaw) {
            pairRow.setAttribute('data-status', 'complete');
            header.style.background = 'linear-gradient(135deg, #d4edda 0%, #c3e6cb 100%)';
            header.style.borderLeft = '3px solid #28a745';
            if (statusText) {
                if (isBatchMode) {
                    // Main title is the source (raw) file being matched; the batch name is secondary info only.
                    statusText.innerHTML = '<i class="fas fa-check-circle text-success me-1"></i>' + rawName +
                        ' <span class="text-muted" style="font-size: 0.6rem;">(' + masterName + ')</span>';
                } else {
                    statusText.innerHTML = '<i class="fas fa-check-circle text-success me-1"></i>' + masterName;
                }
            }
            if (statusIcon) statusIcon.innerHTML = '';
        } else if (hasMaster || hasRaw) {
            pairRow.setAttribute('data-status', 'partial');
            header.style.background = 'linear-gradient(135deg, #fff3cd 0%, #ffeeba 100%)';
            header.style.borderLeft = '3px solid #ffc107';
            if (statusText) statusText.innerHTML = hasMaster ? masterName : (hasRaw ? rawName : '');
            if (statusIcon) statusIcon.innerHTML = '<i class="fas fa-exclamation-triangle text-warning" style="font-size: 0.7rem;" title="Missing ' + (hasMaster ? 'raw file' : 'master') + '"></i>';
        } else {
            pairRow.setAttribute('data-status', 'incomplete');
            header.style.background = '';
            header.style.borderLeft = '';
            if (statusText) statusText.innerHTML = '';
            if (statusIcon) statusIcon.innerHTML = '';
        }
    }
}

function toggleMasterInput(pairIndex, sourceType) {
    const pairRow = document.querySelector('[data-pair-index="' + pairIndex + '"]');
    if (!pairRow) {
        console.log('[toggleMasterInput] Pair row not found for index:', pairIndex);
        return;
    }
    
    const fileWrapper = pairRow.querySelector('.master-file-wrapper');
    const batchWrapper = pairRow.querySelector('.master-batch-wrapper');
    const masterInput = pairRow.querySelector('.master-input');
    
    console.log('[toggleMasterInput] pairIndex=' + pairIndex + ', sourceType=' + sourceType);
    
    if (sourceType === 'batch') {
        // Hide file input, show batch select
        if (fileWrapper) {
            fileWrapper.style.display = 'none';
            fileWrapper.classList.add('d-none');
        }
        if (batchWrapper) {
            batchWrapper.style.display = 'block';
            batchWrapper.classList.remove('d-none');
        }
        if (masterInput) {
            masterInput.removeAttribute('required');
            masterInput.disabled = true; // DISABLE to prevent browser validation
            masterInput.value = ''; // Clear any selected file
        }
    } else {
        // Show file input, hide batch select
        if (fileWrapper) {
            fileWrapper.style.display = 'block';
            fileWrapper.classList.remove('d-none');
        }
        if (batchWrapper) {
            batchWrapper.style.display = 'none';
            batchWrapper.classList.add('d-none');
        }
        if (masterInput) {
            masterInput.disabled = false; // Re-enable for file mode
        }
    }
    
    // Update status after source change
    updatePairStatus(pairIndex);
}

function initializeBatchSelection(pairIndex) {
    const selectorContainer = document.getElementById('master-source-selector-' + pairIndex);
    const batchSelect = document.getElementById('batch_id_' + pairIndex);
    const spacer = document.getElementById('raw-spacer-' + pairIndex);
    const fileWrapper = document.getElementById('master-file-wrapper-' + pairIndex);
    const batchWrapper = document.getElementById('master-batch-wrapper-' + pairIndex);
    const pairRow = document.querySelector('[data-pair-index="' + pairIndex + '"]');
    const masterInput = pairRow ? pairRow.querySelector('.master-input') : null;
    
    // Support either local template variable or global window context
    const canUseBatch = typeof canUseBatchMatching !== 'undefined' ? canUseBatchMatching : (window.canUseBatchMatching || false);
    const batches = typeof batchesData !== 'undefined' ? batchesData : (window.smartMatcherBatches || []);

    console.log('[initializeBatchSelection] pairIndex=' + pairIndex + ', canUseBatch=' + canUseBatch + ', batches=' + batches.length);
    
    // Only show batch option if user has permission
    if (selectorContainer && canUseBatch && batches.length > 0) {
        // Add source selector dropdown with compact styling - default to "Use Batch"
        selectorContainer.innerHTML = '<select class="form-select form-select-sm master-source-select mb-1" name="master_source_' + pairIndex + '" onchange="toggleMasterInput(' + pairIndex + ', this.value)" style="font-size: 0.75rem;"><option value="file">Upload File</option><option value="batch" selected>Use Batch</option></select>';
        
        // Add spacer for alignment
        if (spacer) {
            spacer.innerHTML = '<div style="height: 31px;"></div>';
        }            
        // Populate batch dropdown
        if (window.setupHierarchicalSelectors) {
            window.setupHierarchicalSelectors(batchWrapper, pairIndex);
        } else if (batchSelect) {
            let optionsHtml = '<option value="">Choose a batch...</option>';
            batches.forEach(function(batch) {
                optionsHtml += '<option value="' + batch.id + '">' + batch.name + ' (' + batch.count + ' students)</option>';
            });
            batchSelect.innerHTML = optionsHtml;
        };
        
        // Default to batch mode: hide file input, show batch select
        if (fileWrapper) {
            fileWrapper.style.display = 'none';
            fileWrapper.classList.add('d-none');
        }
        if (batchWrapper) {
            batchWrapper.style.display = 'block';
            batchWrapper.classList.remove('d-none');
        }
        // DISABLE the master input to prevent browser validation errors
        if (masterInput) {
            masterInput.disabled = true;
            masterInput.removeAttribute('required');
        }
    } else {
        // No batch permission: show file input, hide batch select
        if (fileWrapper) {
            fileWrapper.style.display = 'block';
            fileWrapper.classList.remove('d-none');
        }
        if (batchWrapper) {
            batchWrapper.style.display = 'none';
            batchWrapper.classList.add('d-none');
        }
        if (masterInput) {
            masterInput.disabled = false;
        }
    }
}

// Expose functions to window scope for inline onclick/onchange handlers
window.togglePairCollapse = togglePairCollapse;
window.updatePairStatus = updatePairStatus;
window.toggleMasterInput = toggleMasterInput;
window.initializeBatchSelection = initializeBatchSelection;
window.initializeSmartMatcher = initializeSmartMatcher;
