const driveConnectButton = document.querySelector("#connect-drive");
const driveConnectionStatus = document.querySelector("#drive-connection-status");

async function updateDriveStatus() {
  try {
    const response = await fetch("/api/drive/status");
    const status = await response.json();
    if (!response.ok) {
      throw new Error(status.detail || `Request failed (${response.status})`);
    }
    driveConnectionStatus.textContent = status.message;
    driveConnectButton.disabled = !status.configured;
    driveConnectButton.textContent = status.connected
      ? "Reconnect Google Drive"
      : "Connect Google Drive";
  } catch (error) {
    driveConnectionStatus.textContent = error.message;
    driveConnectButton.disabled = true;
  }
}

driveConnectButton.addEventListener("click", async () => {
  driveConnectButton.disabled = true;
  driveConnectionStatus.textContent =
    "Opening Google sign-in. Complete authorization in the browser window…";
  let connectError = "";
  try {
    const response = await fetch("/api/drive/connect", { method: "POST" });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.detail || `Request failed (${response.status})`);
    }
    driveConnectionStatus.textContent = result.message;
  } catch (error) {
    connectError = error.message;
  }
  await updateDriveStatus();
  if (connectError) driveConnectionStatus.textContent = connectError;
});

updateDriveStatus();
