-- Clicks "Open" in Gatekeeper's first-launch question (the window belongs to CoreServicesUIAgent).
-- Prints what it did, for gatekeeper.log.
tell application "System Events"
	if not (exists process "CoreServicesUIAgent") then return "no CoreServicesUIAgent process"
	tell process "CoreServicesUIAgent"
		if (count of windows) is 0 then return "no Gatekeeper window"
		repeat with w in windows
			repeat with e in (entire contents of w)
				try
					if (role of e is "AXButton") and (name of e is "Open") then
						click e
						return "clicked Open"
					end if
				end try
			end repeat
		end repeat
		return "window found, no Open button"
	end tell
end tell
