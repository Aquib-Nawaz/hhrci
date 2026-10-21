#!/bin/sh
cd "$(dirname "$0")"
python3 start_mail_desk.py
status=$?
if [ "$status" -ne 0 ]; then
    printf "\nMail Desk did not start. Press Return to close this window."
    read -r _
fi
exit "$status"
