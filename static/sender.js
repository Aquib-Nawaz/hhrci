const mappingList = document.querySelector("#mapping-list");
const mappingStatus = document.querySelector("#mapping-status");
const testRunSelect = document.querySelector("#test-run");
const sendButton = document.querySelector("#send-mappings");
const sendStatus = document.querySelector("#send-status");
const sendResults = document.querySelector("#send-results");
const testRunSummary = document.querySelector("#test-run-summary");
const uploadFolderLabel = document.querySelector("#upload-folder");
const senderAccount = document.querySelector("#sender-account");
const discoveryStatus = document.querySelector("#discovery-status");
const discoveryResults = document.querySelector("#discovery-results");
let uploadFolderPath = "";

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

function addMappingRow(mapping = {}) {
  const row = document.createElement("tr");
  for (const [field, placeholder] of [
    ["roll_number", "e.g. 00095"],
    ["mail_account", "recipient@example.com"],
    ["sent", "pending / sent"],
  ]) {
    const cell = document.createElement("td");
    const input = document.createElement("input");
    input.type = "text";
    input.dataset.field = field;
    input.value = mapping[field] || "";
    input.placeholder = placeholder;
    input.setAttribute("aria-label", field.replaceAll("_", " "));
    cell.append(input);
    row.append(cell);
  }
  const actionCell = document.createElement("td");
  const removeButton = document.createElement("button");
  removeButton.type = "button";
  removeButton.className = "button button-danger";
  removeButton.textContent = "Remove";
  removeButton.addEventListener("click", () => row.remove());
  actionCell.append(removeButton);
  row.append(actionCell);
  mappingList.append(row);
}

function collectMappings() {
  return [...mappingList.querySelectorAll("tr")].map((row) => {
    return Object.fromEntries(
      [...row.querySelectorAll("input")].map((input) => [
        input.dataset.field,
        input.value.trim(),
      ]),
    );
  }).filter((row) => row.roll_number || row.mail_account || row.sent);
}

async function loadMappings() {
  const { rows } = await api("/api/mappings");
  mappingList.replaceChildren();
  rows.forEach(addMappingRow);
  if (!rows.length) addMappingRow();
}

async function loadTests(selectedId = testRunSelect.value) {
  const { tests } = await api("/api/tests");
  testRunSelect.replaceChildren(new Option("Choose a saved run", ""));
  for (const test of tests) {
    const label = `${new Date(test.created_at).toLocaleString()} · ${test.counts.found} found · ${test.counts.downloaded} downloaded · ${test.counts.updated} updated`;
    testRunSelect.add(new Option(label, test.id));
  }
  if (tests.some((test) => test.id === selectedId)) testRunSelect.value = selectedId;
  updateSendButton();
  return tests;
}

async function loadAccounts() {
  const { accounts } = await api("/api/accounts");
  senderAccount.replaceChildren(new Option("Choose a sender account", ""));
  for (const account of accounts) senderAccount.add(new Option(account, account));
  if (accounts.length === 1) senderAccount.value = accounts[0];
  updateSendButton();
}

function updateSendButton() {
  sendButton.disabled =
    !testRunSelect.value || !uploadFolderPath || !senderAccount.value;
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
  uploadFolderPath = upload.folder || "";
  uploadFolderLabel.textContent = uploadFolderPath || "No folder selected";
  if (upload.sender_account) senderAccount.value = upload.sender_account;
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
  const progress = upload.progress;
  const state = upload.status === "running"
    ? `Uploading ${progress.completed}/${progress.total}`
    : `Upload ${upload.status.replaceAll("_", " ")}`;
  testRunSummary.textContent =
    `Test ${testRun.id} · ${counts.found} found · ${counts.downloaded} downloaded (${counts.downloaded_files} files) · ${counts.updated} updated · ${state}`;
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
senderAccount.addEventListener("change", updateSendButton);

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
  if (!testRunSelect.value) return;
  sendButton.disabled = true;
  sendStatus.textContent = "Starting uploads…";
  sendResults.hidden = true;
  try {
    const testRun = await api(`/api/tests/${testRunSelect.value}/send-mappings`, {
      method: "POST",
      body: JSON.stringify({
        folder: uploadFolderPath,
        sender_account: senderAccount.value,
      }),
    });
    displayProgress(testRun);
    await pollUpload(testRun.id);
  } catch (error) {
    sendStatus.textContent = error.message;
  } finally {
    updateSendButton();
  }
});

async function initialize() {
  try {
    const requestedId = new URLSearchParams(window.location.search).get("testId");
    const [, tests] = await Promise.all([
      loadMappings(),
      loadTests(requestedId || ""),
      loadAccounts(),
    ]);
    const activeTest = tests.find((test) => activeOperation(test));
    const selectedId = requestedId || activeTest?.id;
    if (!selectedId) return;

    testRunSelect.value = selectedId;
    await showSavedTest(selectedId);
  } catch (error) {
    mappingStatus.textContent = error.message;
  }
}

initialize();
