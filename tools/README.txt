ExamPartner — hardware survey
=============================

WHAT THIS IS
A small read-only tool that records what hardware identifiers this computer
reports. It is used to design ExamPartner's Windows licensing so that
reformatting or repairing a PC does not cost the school a licence.

WHAT IT DOES NOT DO
  - It does not install anything.
  - It does not activate or license anything.
  - It does not connect to the internet or send data anywhere.
  - It does not change any setting on this computer.
  - It does not need an administrator password.

It reads values Windows already exposes (motherboard and BIOS details, and
similar) and saves them to a file in its own folder. That is all.

HOW TO RUN IT
  1. Copy this whole folder to the computer, or run it from the USB stick.
  2. Double-click  Run-Collector.bat
  3. Type a label for the machine when asked, for example:  LAB-PC-07
  4. Press Enter, wait a moment, then press Enter again to close.
  5. Repeat on the next computer, using a different label.

Use a different label for every computer. The labels are how the results are
told apart, and using the same one twice makes two machines look like one.

WHEN YOU ARE FINISHED
Send back the whole  survey-output  folder. It contains one JSON file per
computer plus a single survey-summary.csv listing them all.

IF A MACHINE SAYS "PROBLEM ... the reading could not be taken"
That means the tool could not read the hardware on that computer, not that the
computer has unusual hardware. Try running it again on that machine. If it
still fails, note which machine it was and send the folder anyway.

    NOTE FOR ANALYSIS (Nitoni): rows with collection_status = INCOMPLETE are
    NOT evidence about hardware. Exclude them before drawing any conclusion
    about uniqueness, and re-collect those machines. Treating a failed reading
    as "this machine has no identifiers" would point the licensing design in
    exactly the wrong direction.

IF IT WILL NOT RUN
Open PowerShell in this folder and run:

    powershell -ExecutionPolicy Bypass -File Collect-Fingerprint.ps1

If that also fails, send the error message back rather than trying to fix it.

Questions: contact Nitoni.
