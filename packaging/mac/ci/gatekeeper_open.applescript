-- Clicks "Open" in Gatekeeper's first-launch question (the window belongs to CoreServicesUIAgent).
-- Prints every button it saw and what it did, for gatekeeper.log.
set seen to ""
tell application "System Events"
	if not (exists process "CoreServicesUIAgent") then return "no CoreServicesUIAgent process"
	tell process "CoreServicesUIAgent"
		set frontmost to true
		if (count of windows) is 0 then return "no Gatekeeper window"
		repeat with w in windows
			repeat with e in (entire contents of w)
				if class of e is button then
					set t to ""
					try
						set t to t & (name of e as text)
					end try
					try
						set t to t & "|" & (title of e as text)
					end try
					try
						set t to t & "|" & (description of e as text)
					end try
					set seen to seen & "[" & t & "] "
					if t contains "Open" then
						click e
						return "clicked Open; buttons: " & seen
					end if
				end if
			end repeat
		end repeat
	end tell
end tell
return "no Open button; buttons: " & seen
