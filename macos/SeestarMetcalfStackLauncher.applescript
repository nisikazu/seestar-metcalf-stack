on run
    display dialog "Drag one Seestar subframe folder onto this application." buttons {"OK"} default button "OK"
end run

on open droppedItems
    if (count of droppedItems) is not 1 then
        display dialog "Drop exactly one Seestar subframe folder." buttons {"OK"} default button "OK"
        return
    end if

    set appBundle to POSIX path of (path to me)
    set appRoot to do shell script "/usr/bin/dirname " & quoted form of (text 1 thru -2 of appBundle)
    set launcherPath to appRoot & "/seestar-metcalf-stack.sh"
    set sourcePath to POSIX path of item 1 of droppedItems
    set shellCommand to "cd " & quoted form of appRoot & " && " & quoted form of launcherPath & " " & quoted form of sourcePath & "; status=$?; echo; if [ $status -eq 0 ]; then echo 'Processing complete.'; else echo 'Processing failed with exit code '$status'.'; fi; echo 'Press Return to close.'; read -r _; exit $status"

    tell application "Terminal"
        activate
        do script shellCommand
    end tell
end open
