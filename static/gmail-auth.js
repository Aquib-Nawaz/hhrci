const gmailConnectButton = document.querySelector("#connect-gmail");
const gmailConnectionStatus = document.querySelector("#gmail-connection-status");

async function updateGmailStatus() {
  try {
    const response = await fetch("/api/gmail/status");
    const status = await response.json();
    if (!response.ok) {
      throw new Error(status.detail || `Request failed (${response.status})`);
    }
    gmailConnectionStatus.textContent = status.message;
    gmailConnectButton.disabled = !status.configured;
    gmailConnectButton.textContent = status.connected
      ? "Reconnect Gmail API"
      : "Connect Gmail API";
  } catch (error) {
    gmailConnectionStatus.textContent = error.message;
    gmailConnectButton.disabled = true;
  }
}

gmailConnectButton.addEventListener("click", async () => {
  gmailConnectButton.disabled = true;
  gmailConnectionStatus.textContent =
    "Opening Google sign-in. Complete authorization in the browser window…";
  let connectError = "";
  try {
    const response = await fetch("/api/gmail/connect", { method: "POST" });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.detail || `Request failed (${response.status})`);
    }
    gmailConnectionStatus.textContent = result.message;
  } catch (error) {
    connectError = error.message;
  }
  await updateGmailStatus();
  if (connectError) gmailConnectionStatus.textContent = connectError;
});

updateGmailStatus();
