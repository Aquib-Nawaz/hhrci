const mappingList = document.querySelector("#mapping-list");
const mappingStatus = document.querySelector("#mapping-status");
const testRunSelect = document.querySelector("#test-run");
const sendButton = document.querySelector("#send-mappings");
const sendStatus = document.querySelector("#send-status");
const sendResults = document.querySelector("#send-results");
const testRunSummary = document.querySelector("#test-run-summary");
const uploadFolderLabel = document.querySelector("#upload-folder");
const senderAccount = document.querySelector("#sender-account");
const uploadSubject = document.querySelector("#upload-subject");
const discoveryStatus = document.querySelector("#discovery-status");
const discoveryResults = document.querySelector("#discovery-results");
const fileStatusList = document.querySelector("#file-status-list");
const fileStatusSummary = document.querySelector("#file-status-summary");
const fileStatusEmpty = document.querySelector("#file-status-empty");
const sendMode = document.querySelector("#send-mode");
const studentMappingPanel = document.querySelector("#student-mapping-panel");
const studentDiscoveryControls = document.querySelector("#student-discovery-controls");
const evaluatorPanel = document.querySelector("#evaluator-panel");
const evaluatorList = document.querySelector("#evaluator-list");
const evaluatorStatus = document.querySelector("#evaluator-status");
const assignmentPreviewButton = document.querySelector("#preview-evaluator-assignments");
const assignmentPreviewStatus = document.querySelector("#assignment-preview-status");
let uploadFolderPath = "";
let uploadInProgress = false;
let evaluatorAssignments = [];
let stagedFileStatus = [];
let excludedFiles = new Set();
let uploadPreviewReady = false;
let savedUploadRuns = [];
let evaluatorSaveTimer = null;
let evaluatorSaveRevision = 0;
let evaluatorSaveQueue = Promise.resolve();

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || `Request failed (${response.status})`);
  return result;
}

function delay(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function clearAssignmentPreview() {
  evaluatorAssignments = [];
  stagedFileStatus = [];
  excludedFiles = new Set();
  uploadPreviewReady = false;
  fileStatusList?.replaceChildren();
  if (fileStatusEmpty) fileStatusEmpty.hidden = false;
  if (fileStatusSummary) fileStatusSummary.textContent = "Files in this upload test";
  if (assignmentPreviewStatus) assignmentPreviewStatus.textContent = "";
  updateSendButton();
}

function persistEvaluators(revision, rows) {
  const save = evaluatorSaveQueue.catch(() => {}).then(async () => {
    const result = await api("/api/evaluators", {
      method: "PUT",
      body: JSON.stringify({ rows }),
    });
    if (revision === evaluatorSaveRevision) {
      evaluatorStatus.textContent =
        `Evaluator list saved to data/evaluators.csv (${result.rows.length} evaluator(s)).`;
    }
    return result.rows;
  });
  evaluatorSaveQueue = save;
  return save;
}

function scheduleEvaluatorSave() {
  window.clearTimeout(evaluatorSaveTimer);
  const revision = ++evaluatorSaveRevision;
  const rows = collectEvaluators();
  const incomplete = rows.some((row) =>
    !row.name ||
    !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(row.email),
  );
  if (incomplete) {
    evaluatorStatus.textContent =
      "Enter both a name and valid email; the completed list saves automatically.";
    return;
  }
  evaluatorStatus.textContent = "Saving evaluator list…";
  evaluatorSaveTimer = window.setTimeout(() => {
    persistEvaluators(revision, rows).catch((error) => {
      if (revision === evaluatorSaveRevision) {
        evaluatorStatus.textContent = error.message;
      }
    });
  }, 500);
}

async function saveEvaluatorsNow() {
  window.clearTimeout(evaluatorSaveTimer);
  const revision = ++evaluatorSaveRevision;
  evaluatorStatus.textContent = "Saving evaluator list…";
  return persistEvaluators(revision, collectEvaluators());
}

function addMappingRow(mapping = {}) {
  const row = document.createElement("tr");
  for (const [field, placeholder] of [
    ["roll_number", "e.g. 00095"],
    ["mail_account", "recipient@example.com"],
  ]) {
    const cell = document.createElement("td");
    const input = document.createElement("input");
    input.type = "text";
    input.dataset.field = field;
    input.value = mapping[field] || "";
    input.placeholder = placeholder;
    input.setAttribute("aria-label", field.replaceAll("_", " "));
    input.addEventListener("input", updateSendButton);
    input.addEventListener("input", clearAssignmentPreview);
    cell.append(input);
    row.append(cell);
  }
  const actionCell = document.createElement("td");
  const removeButton = document.createElement("button");
  removeButton.type = "button";
  removeButton.className = "button button-danger";
  removeButton.textContent = "Remove";
  removeButton.addEventListener("click", () => {
    row.remove();
    clearAssignmentPreview();
    updateSendButton();
  });
  actionCell.append(removeButton);
  row.append(actionCell);
  mappingList.append(row);
}

function addEvaluatorRow(evaluator = {}) {
  const row = document.createElement("tr");
  for (const [field, placeholder] of [
    ["name", "e.g. Evaluator 1"],
    ["email", "evaluator@example.com"],
  ]) {
    const cell = document.createElement("td");
    const input = document.createElement("input");
    input.type = "text";
    input.dataset.field = field;
    input.value = evaluator[field] || "";
    input.placeholder = placeholder;
    input.setAttribute("aria-label", `Evaluator ${field}`);
    input.addEventListener("input", () => {
      clearAssignmentPreview();
      scheduleEvaluatorSave();
      updateSendButton();
    });
    cell.append(input);
    row.append(cell);
  }
  const actionCell = document.createElement("td");
  const removeButton = document.createElement("button");
  removeButton.type = "button";
  removeButton.className = "button button-danger";
  removeButton.textContent = "Remove";
  removeButton.addEventListener("click", () => {
    row.remove();
    clearAssignmentPreview();
    scheduleEvaluatorSave();
    updateSendButton();
  });
  actionCell.append(removeButton);
  row.append(actionCell);
  evaluatorList.append(row);
  updateSendButton();
}

function collectMappings() {
  return [...mappingList.querySelectorAll("tr")].map((row) => {
    return Object.fromEntries(
      [...row.querySelectorAll("input")].map((input) => [
        input.dataset.field,
        input.value.trim(),
      ]),
    );
  }).filter((row) => row.roll_number || row.mail_account);
}

function collectEvaluators() {
  return [...evaluatorList.querySelectorAll("tr")].map((row) =>
    Object.fromEntries(
      [...row.querySelectorAll("input")].map((input) => [
        input.dataset.field,
        input.value.trim(),
      ]),
    ),
  ).filter((row) => row.name || row.email);
}

async function loadMappings() {
  const { rows } = await api("/api/mappings");
  mappingList.replaceChildren();
  rows.forEach(addMappingRow);
  if (!rows.length) addMappingRow();
}

async function loadEvaluators() {
  const { rows } = await api("/api/evaluators");
  evaluatorList.replaceChildren();
  rows.forEach(addEvaluatorRow);
  if (!rows.length) addEvaluatorRow();
  evaluatorStatus.textContent = rows.length
    ? `Loaded ${rows.length} saved evaluator(s) from data/evaluators.csv.`
    : "Evaluator names and emails will be saved automatically as you enter them.";
}

async function loadTests(selectedId = testRunSelect.value) {
  const { tests } = await api("/api/upload-runs");
  savedUploadRuns = tests;
  testRunSelect.replaceChildren(new Option("Choose an upload run", ""));
  for (const test of tests) {
    const label = `${test.test_name || new Date(test.created_at).toLocaleString()} · ${test.upload_mode} · ${test.counts.updated} sent · ${test.pending_file_count} pending · ${test.operations.upload.status.replaceAll("_", " ")}`;
    testRunSelect.add(new Option(label, test.id));
  }
  if (tests.some((test) => test.id === selectedId)) testRunSelect.value = selectedId;
  updateSendButton();
  return tests;
}

function selectedResumeTestId() {
  const selectedId = testRunSelect.value;
  if (!selectedId) return "";
  const inputName = document.querySelector("#test-name").value.trim().toLocaleLowerCase();
  const selectedRun = savedUploadRuns.find((run) => run.id === selectedId);
  return selectedRun?.test_name?.trim().toLocaleLowerCase() === inputName
    ? selectedId
    : "";
}

async function loadAccounts() {
  const { accounts } = await api("/api/accounts");
  senderAccount.replaceChildren(new Option("Choose a sender account", ""));
  for (const account of accounts) senderAccount.add(new Option(account, account));
  if (accounts.length === 1) senderAccount.value = accounts[0];
  updateSendButton();
}

function updateSendButton() {
  const readyForMode = sendMode.value === "evaluator"
    ? evaluatorAssignments.length > 0
    : collectMappings().some((mapping) =>
      /^\d{5}$/.test(mapping.roll_number) &&
      /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(mapping.mail_account),
    );
  const pendingFiles = stagedFileStatus.filter((file) =>
    !["sent", "removed", "ignored_no_roll_number", "ambiguous"].includes(file.status) &&
    /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(file.recipient || ""),
  );
  sendButton.disabled =
    uploadInProgress ||
    !uploadFolderPath ||
    !senderAccount.value ||
    !document.querySelector("#test-name").value.trim() ||
    !readyForMode ||
    !uploadPreviewReady ||
    !pendingFiles.length;
}

function updateSendModePanels() {
  const isEvaluatorMode = sendMode.value === "evaluator";
  clearAssignmentPreview();
  studentMappingPanel.hidden = isEvaluatorMode;
  studentDiscoveryControls.hidden = isEvaluatorMode;
  evaluatorPanel.hidden = !isEvaluatorMode;
  updateSendButton();
}

function activeOperation(test) {
  return ["search", "download", "upload"].find(
    (name) => test.operations?.[name]?.status === "running",
  );
}

async function showSavedTest(testId) {
  const testRun = await api(`/api/tests/${testId}`);
  const operation = activeOperation(testRun);
  if (operation === "search" || operation === "download") {
    window.location.replace(`/?testId=${encodeURIComponent(testId)}`);
    return;
  }

  const upload = testRun.upload_fields || {};
  document.querySelector("#test-name").value = testRun.test_name || "";
  sendMode.value = upload.send_mode || "student";
  updateSendModePanels();
  uploadFolderPath = upload.folder || "";
  uploadFolderLabel.textContent = uploadFolderPath || "No folder selected";
  uploadSubject.value = upload.subject_template || "Attachment for roll number {roll_number}";
  if (upload.sender_account) senderAccount.value = upload.sender_account;
  evaluatorAssignments = upload.assignments || [];
  stagedFileStatus = upload.file_status || [];
  excludedFiles = new Set(upload.excluded_files || []);
  uploadPreviewReady = stagedFileStatus.length > 0;
  updateSendButton();
  displayProgress(testRun);

  if (operation === "upload") {
    await pollUpload(testId);
  } else if (upload.folder && upload.sender_account) {
    sendStatus.textContent = "Saved upload settings restored.";
  }
}

function displayProgress(testRun) {
  const counts = testRun.counts;
  const upload = testRun.operations.upload;
  stagedFileStatus = testRun.upload_fields?.file_status || [];
  excludedFiles = new Set(
    testRun.upload_fields?.excluded_files ||
      stagedFileStatus
        .filter((file) => file.status === "removed")
        .map((file) => file.file),
  );
  const progress = upload.progress;
  const state = upload.status === "running"
    ? `Uploading ${progress.completed}/${progress.total}`
    : `Upload ${upload.status.replaceAll("_", " ")}`;
  testRunSummary.textContent =
    `Test ${testRun.test_name || testRun.id} · ${testRun.upload_fields?.send_mode || "student"} mode · ${counts.updated} files sent · ${state}`;
  renderFileStatus(
    testRun.upload_fields?.file_status || [],
    uploadPreviewReady && upload.status !== "running",
  );
}

function renderFileStatus(files, allowRemoval = false) {
  if (!fileStatusList || !fileStatusEmpty || !fileStatusSummary) {
    if (assignmentPreviewStatus) {
      assignmentPreviewStatus.textContent =
        "The file preview area is missing. Reload the upload page to update it.";
    }
    return;
  }
  fileStatusList.replaceChildren();
  const sentCount = files.filter((file) => file.status === "sent").length;
  const pendingCount = files.filter((file) =>
    !["sent", "removed", "ignored_no_roll_number", "ambiguous"].includes(file.status),
  ).length;
  const removedCount = files.filter((file) => file.status === "removed").length;
  fileStatusSummary.textContent =
    `Files in this upload test (${sentCount} sent · ${pendingCount} pending · ${removedCount} removed)`;
  fileStatusEmpty.hidden = files.length > 0;
  const visibleFiles = allowRemoval
    ? files.filter((file) => file.status !== "removed")
    : files;
  fileStatusEmpty.textContent = uploadPreviewReady
    ? "No files remain in the upload list."
    : "Choose a folder and generate the preview to see files and recipients.";
  for (const file of visibleFiles) {
    const row = document.createElement("tr");
    for (const value of [
      file.roll_number || "—",
      file.file,
      file.evaluator_name
        ? `${file.evaluator_name} (${file.recipient || "no email"})`
        : file.recipient || "—",
      (file.status || "not sent").replaceAll("_", " "),
    ]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    const actionCell = document.createElement("td");
    if (
      allowRemoval &&
      !["sent", "ignored_no_roll_number", "ambiguous"].includes(file.status)
    ) {
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "button button-danger";
      removeButton.textContent = "Remove";
      removeButton.setAttribute("aria-label", `Remove ${file.file} from this upload`);
      removeButton.addEventListener("click", () => removeFileFromPreview(file.file));
      actionCell.append(removeButton);
    }
    row.append(actionCell);
    fileStatusList.append(row);
  }
  fileStatusEmpty.hidden = visibleFiles.length > 0;
  updateSendButton();
}

function removeFileFromPreview(filepath) {
  excludedFiles.add(filepath);
  stagedFileStatus = stagedFileStatus.map((file) =>
    file.file === filepath ? { ...file, status: "removed" } : file,
  );
  evaluatorAssignments = evaluatorAssignments.filter(
    (assignment) => assignment.file !== filepath,
  );
  renderFileStatus(stagedFileStatus, true);
}

async function pollUpload(testId) {
  let testRun;
  do {
    testRun = await api(`/api/tests/${testId}`);
    displayProgress(testRun);
    const operation = testRun.operations.upload;
    if (operation.status === "running") {
      sendStatus.textContent =
        `Uploading ${operation.progress.completed}/${operation.progress.total}…`;
      await delay(850);
    }
  } while (testRun.operations.upload.status === "running");

  const operation = testRun.operations.upload;
  const sentThisRun = Math.max(
    0,
    operation.progress.completed - operation.errors.length - operation.unmatched.length,
  );
  const lines = [
    `Sent this run: ${sentThisRun}`,
    `No file found: ${operation.unmatched.length ? operation.unmatched.join(", ") : "none"}`,
    ...operation.errors.map((error) =>
      `${error.roll_number || ""} (${error.recipient || ""}): ${error.message}${error.files ? ` [${error.files.join(", ")}]` : ""}`,
    ),
  ];
  sendResults.textContent = lines.join("\n");
  sendResults.hidden = false;
  sendStatus.textContent = `Upload ${operation.status.replaceAll("_", " ")}.`;
  await loadTests(testId);
}

document.querySelector("#add-row").addEventListener("click", () => addMappingRow());
document.querySelector("#add-evaluator").addEventListener("click", () => {
  clearAssignmentPreview();
  addEvaluatorRow();
});
senderAccount.addEventListener("change", () => {
  clearAssignmentPreview();
  updateSendButton();
});
sendMode.addEventListener("change", updateSendModePanels);

document.querySelector("#pick-upload-folder").addEventListener("click", async () => {
  discoveryStatus.textContent = "Opening folder picker…";
  try {
    const { folder } = await api("/api/folders/pick?purpose=upload");
    if (!folder) {
      discoveryStatus.textContent = "Folder selection cancelled.";
      return;
    }
    uploadFolderPath = folder;
    uploadFolderLabel.textContent = folder;
    clearAssignmentPreview();
    discoveryStatus.textContent = "";
    updateSendButton();
  } catch (error) {
    discoveryStatus.textContent = error.message;
  }
});

document.querySelector("#discover-mappings").addEventListener("click", async () => {
  discoveryStatus.textContent = "Scanning folder…";
  discoveryResults.hidden = true;
  try {
    if (!uploadFolderPath) throw new Error("Choose the folder containing attachments first.");
    const result = await api("/api/mappings/discover", {
      method: "POST",
      body: JSON.stringify({ folder: uploadFolderPath }),
    });
    clearAssignmentPreview();
    mappingList.replaceChildren();
    result.rows.forEach(addMappingRow);
    if (!result.rows.length) addMappingRow();
    const fileMessages = result.files.map((item) =>
      item.status === "ambiguous"
        ? `${item.roll_number}: ambiguous (${item.files.join(", ")})`
        : `${item.roll_number}: ${item.files[0]}`,
    );
    if (result.ignored_files.length) {
      fileMessages.push(`Ignored (not exactly one 5-digit roll number): ${result.ignored_files.join(", ")}`);
    }
    discoveryResults.textContent = fileMessages.length
      ? fileMessages.join("\n")
      : "No files with exactly one 5-digit roll number were found.";
    discoveryResults.hidden = false;
    discoveryStatus.textContent = `Generated ${result.rows.length} mapping row(s). Enter recipient emails and save.`;
  } catch (error) {
    discoveryStatus.textContent = error.message;
  }
});

document.querySelector("#save-mappings").addEventListener("click", async () => {
  mappingStatus.textContent = "Saving…";
  try {
    const { rows } = await api("/api/mappings", {
      method: "PUT",
      body: JSON.stringify({ rows: collectMappings() }),
    });
    mappingList.replaceChildren();
    rows.forEach(addMappingRow);
    if (!rows.length) addMappingRow();
    mappingStatus.textContent = "CSV saved.";
  } catch (error) {
    mappingStatus.textContent = error.message;
  }
});

document.querySelector("#save-evaluators").addEventListener("click", async () => {
  try {
    await saveEvaluatorsNow();
    clearAssignmentPreview();
  } catch (error) {
    evaluatorStatus.textContent = error.message;
  }
});

assignmentPreviewButton.addEventListener("click", async () => {
  assignmentPreviewButton.disabled = true;
  assignmentPreviewStatus.textContent = "Preparing file and recipient preview…";
  try {
    if (!document.querySelector("#test-name").value.trim()) {
      throw new Error("Enter a test name before previewing files.");
    }
    if (!uploadFolderPath) throw new Error("Choose the folder containing student copies first.");
    if (!senderAccount.value) throw new Error("Choose a Gmail sender account first.");
    if (sendMode.value === "evaluator") {
      await saveEvaluatorsNow();
    } else {
      const { rows } = await api("/api/mappings", {
        method: "PUT",
        body: JSON.stringify({ rows: collectMappings() }),
      });
      mappingList.replaceChildren();
      rows.forEach(addMappingRow);
      if (!rows.length) addMappingRow();
    }
    const preview = await api("/api/upload-preview", {
      method: "POST",
      body: JSON.stringify({
        test_name: document.querySelector("#test-name").value.trim(),
        resume_test_id: selectedResumeTestId(),
        folder: uploadFolderPath,
        sender_account: senderAccount.value,
        subject_template: uploadSubject.value.trim(),
        send_mode: sendMode.value,
        excluded_files: [...excludedFiles],
      }),
    });
    evaluatorAssignments = preview.assignments;
    stagedFileStatus = preview.file_status;
    excludedFiles = new Set(
      stagedFileStatus
        .filter((file) => file.status === "removed")
        .map((file) => file.file),
    );
    uploadPreviewReady = true;
    const fileStatusPanel = document.querySelector("#file-status-panel");
    if (fileStatusPanel) fileStatusPanel.open = true;
    renderFileStatus(stagedFileStatus, true);
    if (assignmentPreviewStatus) {
      assignmentPreviewStatus.textContent =
        `${stagedFileStatus.length - excludedFiles.size} file(s) ready to review. Remove any files you do not want sent.`;
    }
  } catch (error) {
    uploadPreviewReady = false;
    updateSendButton();
    if (assignmentPreviewStatus) {
      assignmentPreviewStatus.textContent = `Could not prepare preview: ${error.message}`;
    } else {
      sendStatus.textContent = `Could not prepare preview: ${error.message}`;
    }
  } finally {
    assignmentPreviewButton.disabled = false;
  }
});

testRunSelect.addEventListener("change", async () => {
  updateSendButton();
  testRunSummary.textContent = "";
  sendResults.hidden = true;
  if (!testRunSelect.value) return;
  try {
    await showSavedTest(testRunSelect.value);
  } catch (error) {
    sendStatus.textContent = error.message;
  }
});

sendButton.addEventListener("click", async () => {
  uploadInProgress = true;
  updateSendButton();
  sendStatus.textContent = "Saving mappings and starting upload…";
  sendResults.hidden = true;
  try {
    if (sendMode.value === "student") {
      const { rows } = await api("/api/mappings", {
        method: "PUT",
        body: JSON.stringify({ rows: collectMappings() }),
      });
      mappingList.replaceChildren();
      rows.forEach(addMappingRow);
      if (!rows.length) addMappingRow();
    } else {
      await saveEvaluatorsNow();
    }
    const testRun = await api("/api/upload-runs", {
      method: "POST",
      body: JSON.stringify({
        test_name: document.querySelector("#test-name").value.trim(),
        resume_test_id: selectedResumeTestId(),
        folder: uploadFolderPath,
        sender_account: senderAccount.value,
        subject_template: uploadSubject.value.trim(),
        send_mode: sendMode.value,
        assignments: evaluatorAssignments,
        excluded_files: [...excludedFiles],
      }),
    });
    await loadTests(testRun.id);
    displayProgress(testRun);
    sendStatus.textContent = "Upload started.";
    await pollUpload(testRun.id);
  } catch (error) {
    sendStatus.textContent = error.message;
  } finally {
    uploadInProgress = false;
    updateSendButton();
  }
});

async function initialize() {
  try {
    const requestedId = new URLSearchParams(window.location.search).get("testId");
    const [, , tests] = await Promise.all([
      loadMappings(),
      loadEvaluators(),
      loadTests(""),
      loadAccounts(),
    ]);
    const activeTest = tests.find((test) => activeOperation(test));
    const selectedId =
      tests.some((test) => test.id === requestedId)
        ? requestedId
        : activeTest?.id;
    if (!selectedId) return;

    testRunSelect.value = selectedId;
    await showSavedTest(selectedId);
  } catch (error) {
    mappingStatus.textContent = error.message;
  }
}

initialize();

document.querySelector("#test-name")?.addEventListener("input", clearAssignmentPreview);
