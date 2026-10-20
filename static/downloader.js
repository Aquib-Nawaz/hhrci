const searchForm = document.querySelector("#search-form");
const accountSelect = document.querySelector("#account");
const messageList = document.querySelector("#message-list");
const testHistory = document.querySelector("#test-history");
const searchStatus = document.querySelector("#search-status");
const downloadStatus = document.querySelector("#download-status");
const runSummary = document.querySelector("#run-summary");
const errorList = document.querySelector("#error-list");
const downloadButton = document.querySelector("#download-selected");
let currentTest = null;
let downloadFolderPath = "";

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.detail || `Request failed (${response.status})`);
  }
  return result;
}

function operationText(operation, label) {
  if (!operation || operation.status === "idle") return `${label}: not started`;
  const { completed, total } = operation.progress;
  if (operation.status === "running") {
    return `${label}: ${completed}/${total} processed`;
  }
  return `${label}: ${operation.status.replaceAll("_", " ")}`;
}

function setMessageRows(messages) {
  messageList.replaceChildren();
  if (!messages.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "empty-state";
    cell.textContent = currentTest?.operations.search.status === "running"
      ? "Searching inboxes…"
      : "No messages in this test run.";
    row.append(cell);
    messageList.append(row);
    downloadButton.disabled =
      !currentTest || currentTest.operations.search.status === "running";
    return;
  }

  for (const message of messages) {
    const row = document.createElement("tr");
    const selectCell = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "message-select";
    checkbox.dataset.account = message.account;
    checkbox.dataset.uid = message.uid;
    checkbox.disabled =
      currentTest.operations.search.status === "running" ||
      currentTest.operations.download.status === "running";
    checkbox.setAttribute("aria-label", `Select ${message.subject || "message"}`);
    selectCell.append(checkbox);
    row.append(selectCell);
    for (const value of [message.sender, message.subject, message.account, message.date]) {
      const cell = document.createElement("td");
      cell.textContent = value || "—";
      row.append(cell);
    }
    const actionCell = document.createElement("td");
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "button button-danger";
    removeButton.textContent = "Remove";
    removeButton.disabled =
      currentTest.operations.search.status === "running" ||
      currentTest.operations.download.status === "running";
    removeButton.title = "Remove from this saved test run; does not delete from Gmail";
    removeButton.addEventListener("click", () => removeMessage(message));
    actionCell.append(removeButton);
    row.append(actionCell);
    messageList.append(row);
  }
  downloadButton.disabled =
    currentTest.operations.search.status === "running" ||
    currentTest.operations.download.status === "running";
}

function displayTest(testRun) {
  currentTest = testRun;
  document.querySelector("#test-name").value = testRun.test_name || "";
  downloadFolderPath = testRun.download_fields?.output_folder || "";
  document.querySelector("#download-folder").textContent =
    downloadFolderPath || "Choose a folder after searching.";
  setMessageRows(testRun.messages);
  const counts = testRun.counts;
  runSummary.textContent = [
    `Test ${testRun.test_name || testRun.id}`,
    `${counts.found} found`,
    `${counts.downloaded} messages downloaded (${counts.downloaded_files} files)`,
    `${counts.updated} updated`,
    operationText(testRun.operations.search, "Search"),
    operationText(testRun.operations.download, "Download"),
    operationText(testRun.operations.upload, "Upload"),
  ].join(" · ");

  const errors = Object.entries(testRun.operations)
    .flatMap(([name, operation]) =>
      (operation.errors || []).map((error) =>
        `${name}: ${error.account || error.attachment_name || error.uid || ""} ${error.message}`,
      ),
    );
  errorList.hidden = errors.length === 0;
  errorList.textContent = errors.join(" | ");
  if (testHistory.value !== testRun.id) testHistory.value = testRun.id;
}

async function loadTests() {
  const { tests } = await api("/api/tests");
  testHistory.replaceChildren(new Option("Choose a saved run", ""));
  for (const test of tests) {
    testHistory.add(new Option(
      `${test.test_name || new Date(test.created_at).toLocaleString()} · ${test.message_count} message(s)${activeOperation(test) ? ` · ${activeOperation(test)}` : ""}`,
      test.id,
    ));
  }
  return tests;
}

function activeOperation(test) {
  return ["search", "download", "upload"].find(
    (name) => test.operations?.[name]?.status === "running",
  );
}

function delay(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function pollTest(testId, operationName) {
  let testRun;
  do {
    testRun = await api(`/api/tests/${testId}`);
    displayTest(testRun);
    const operation = testRun.operations[operationName];
    const { completed, total } = operation.progress;
    const statusText = operation.status === "running"
      ? `${operationName === "search" ? "Searching" : "Downloading"}: ${completed}/${total}`
      : operationText(operation, operationName);
    if (operationName === "search") searchStatus.textContent = statusText;
    else downloadStatus.textContent = statusText;
    if (operation.status === "running") await delay(850);
  } while (testRun.operations[operationName].status === "running");
  return testRun;
}

async function removeMessage(message) {
  if (!currentTest) return;
  try {
    currentTest = await api(
      `/api/tests/${currentTest.id}/messages/${encodeURIComponent(message.account)}/${encodeURIComponent(message.uid)}`,
      { method: "DELETE" },
    );
    displayTest(currentTest);
  } catch (error) {
    window.alert(error.message);
  }
}

async function initialize() {
  const today = new Date();
  today.setDate(today.getDate() - 7);
  document.querySelector("#after-date").value = today.toISOString().slice(0, 10);
  try {
    const { accounts } = await api("/api/accounts");
    if (!accounts.length) {
      searchStatus.textContent = "Connect your Gmail account using the Connect Gmail API button.";
    }
    accountSelect.replaceChildren(new Option("Choose Gmail account", ""));
    for (const account of accounts) accountSelect.add(new Option(account, account));
    if (accounts.length === 1) accountSelect.value = accounts[0];
    const tests = await loadTests();
    const requestedId = new URLSearchParams(window.location.search).get("testId");
    const requestedTest = tests.find((test) => test.id === requestedId);
    const activeTest = tests.find((test) => activeOperation(test));

    if (requestedTest) {
      if (requestedTest.operations.upload?.status === "running") {
        window.location.replace(`/send?testId=${encodeURIComponent(requestedTest.id)}`);
        return;
      }
      currentTest = await api(`/api/tests/${requestedTest.id}`);
      displayTest(currentTest);
      if (currentTest.operations.search.status === "running") {
        await pollTest(currentTest.id, "search");
      }
      if (currentTest.operations.download.status === "running") {
        await pollTest(currentTest.id, "download");
      }
      return;
    }

    if (!activeTest) return;
    if (activeOperation(activeTest) === "upload") {
      window.location.replace(`/send?testId=${encodeURIComponent(activeTest.id)}`);
      return;
    }
    currentTest = await api(`/api/tests/${activeTest.id}`);
    displayTest(currentTest);
    if (currentTest.operations.search.status === "running") {
      await pollTest(currentTest.id, "search");
    }
    if (currentTest.operations.download.status === "running") {
      await pollTest(currentTest.id, "download");
    }
  } catch (error) {
    searchStatus.textContent = error.message;
  }
}

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  searchStatus.textContent = "Starting search…";
  downloadStatus.textContent = "";
  const payload = {
    test_name: document.querySelector("#test-name").value.trim(),
    account: accountSelect.value,
    senders: document.querySelector("#senders").value
      .split(/[,\n]/)
      .map((sender) => sender.trim())
      .filter(Boolean),
    after_date: document.querySelector("#after-date").value,
  };
  try {
    const testRun = await api("/api/tests", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    displayTest(testRun);
    await loadTests();
    testHistory.value = testRun.id;
    await pollTest(testRun.id, "search");
  } catch (error) {
    searchStatus.textContent = error.message;
  }
});

testHistory.addEventListener("change", async () => {
  if (!testHistory.value) return;
  try {
    currentTest = await api(`/api/tests/${testHistory.value}`);
    displayTest(currentTest);
    downloadStatus.textContent = "";
    if (currentTest.operations.upload.status === "running") {
      window.location.assign(`/send?testId=${encodeURIComponent(currentTest.id)}`);
      return;
    }
    if (currentTest.operations.search.status === "running") {
      await pollTest(currentTest.id, "search");
    }
    if (currentTest.operations.download.status === "running") {
      await pollTest(currentTest.id, "download");
    }
  } catch (error) {
    searchStatus.textContent = error.message;
  }
});

downloadButton.addEventListener("click", async () => {
  if (!currentTest) return;
  if (!downloadFolderPath) {
    downloadStatus.textContent = "Choose an attachment download folder first.";
    return;
  }
  const messages = [...document.querySelectorAll(".message-select:checked")].map((input) => ({
    account: input.dataset.account,
    uid: input.dataset.uid,
  }));
  if (!messages.length) {
    downloadStatus.textContent = "Select at least one message.";
    return;
  }
  downloadStatus.textContent = "Starting download…";
  try {
    await api(`/api/tests/${currentTest.id}/download`, {
      method: "POST",
      body: JSON.stringify({
        output_folder: downloadFolderPath,
        messages,
      }),
    });
    await pollTest(currentTest.id, "download");
  } catch (error) {
    downloadStatus.textContent = error.message;
  }
});

initialize();

document.querySelector("#pick-download-folder").addEventListener("click", async () => {
  downloadStatus.textContent = "Opening folder picker…";
  try {
    const { folder } = await api("/api/folders/pick?purpose=download");
    if (!folder) {
      downloadStatus.textContent = "Folder selection cancelled.";
      return;
    }
    downloadFolderPath = folder;
    document.querySelector("#download-folder").textContent = folder;
    downloadStatus.textContent = "";
  } catch (error) {
    downloadStatus.textContent = error.message;
  }
});
