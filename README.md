# airlift-windows

[airlift](https://github.com/0xjohnnydev/airlift) is a PoC sandbox escape for iOS that needs a Mac to work.

airlift-windows brings this to Windows using Python.

The original airlift PoC uses the AirTraffic which is a service to sync media, including Books from macOS to iOS.
airlift abuses the AirTrafficHost.framework to read files on the iOS device by "syncing" it to the Mac, same for writing files.

airlift (and airlift-windows) have only been tested on iOS 27.0 RC (24A435) but it should also work on iOS 27.0 (24A437).
Do NOT expect airlift (and airlift-windows) to work on other iOS versions, You can try but it may cause unexpected behavior.

## Usage

Clone the repo (or download airlift.py) and install iTunes (not from the Microsoft Store) or the Apple Mobile Device Driver and run airlift.py using Python.

You can also import airlift.py in your Python script if you want to use it in your Python tool.

## Paths

File writes were confirmed in the following directories:

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

Reading a file moves it into Media, reads it through AFC and then moves it back.

As of now, airlift (and airlift-windows) do **not** work on the MobileGestalt plist.

## How did you make it work on Windows

I used AI to reverse-engineer the AirTrafficHost.dll that comes with iTunes/The Apple Mobile Device Driver in `C:\Program Files\Common Files\Apple\Mobile Device Support`

Then I told it to write a Python CLI script that uses AirTrafficHost.dll (and its dependencies) and pymobiledevice3 to communicate with the iOS device and run the exploit on Windows.