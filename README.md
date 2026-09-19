# airlift-windows
[airlift](https://github.com/0xjohnnydev/airlift) is a PoC sandbox escape for iOS that needs a Mac to work.

airlift-windows brings this to Windows using Python.

The original airlift PoC uses AirTraffic which is a service to sync media, including Books from macOS to iOS.
airlift abuses the AirTrafficHost.framework to read/write files on the iOS device by "syncing" it to the Mac, and then "syncing" back if writing to a file.

airlift (and airlift-windows) have only been tested on iOS 27.0 RC (24A435) but it should also work on iOS 27.0 (24A437).
Do NOT expect airlift (and airlift-windows) to work on other iOS versions, You can try but it may cause unexpected behavior.

## Usage
Install [Python](https://www.python.org/downloads/) and run this inside a new terminal window:
```
pip install pymobiledevice3
```

Or if it dosent see the 'pip' command:

```
python -m pip install pymobiledevice3
```

Then install [iTunes](https://support.apple.com/106372) (not from the Microsoft Store) and download 'airlift.py' from this repo (or clone the repo), and run 'airlift.py' using Python:

```
python airlift.py
```

Which will output instructions on how to use airlift.py.

You can also 'import airlift' if you want to use it in your Python tool.

## Paths
Fresh-file writes were confirmed in the following directories:

```text
/var/mobile
/var/mobile/Documents
/var/mobile/Library
/var/mobile/Library/Preferences
/var/mobile/Library/Caches
/var/mobile/Library/SpringBoard
/var/mobile/Library/SMS
/var/mobile/Library/Safari
/var/mobile/Containers
/var/mobile/Containers/Data/Application
/var/mobile/Containers/Shared/AppGroup
/var/tmp
```

Reading a file moves it to Media on the iOS device using the exploit, copies it through AFC to the computer, and then moves the file back to where it was originally on the iOS device.

As of now, airlift (and airlift-windows) do **not** work on the MobileGestalt plist.

## How did you make it work on Windows
I used AI to reverse-engineer the AirTrafficHost.dll that comes with the Apple Mobile Device USB Driver (or its something else but it comes with iTunes) in `C:\Program Files\Common Files\Apple\Mobile Device Support`

Then I told it to write a Python CLI script that uses AirTrafficHost.dll (and its dependencies) and pymobiledevice3 to communicate with the iOS device and run the exploit on Windows.
