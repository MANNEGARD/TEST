# SMGD DriveLog – Privacy Policy

SMGD DriveLog is a private, self-hosted trip journal. Each installation is run by a vehicle
owner on their own server and only handles data about that owner and their own vehicle. The
owner is the one responsible for the data in their installation.

## What is stored

* Vehicle data read from the Volvo Cars APIs with the owner's consent: vehicle identity,
  odometer, fuel and battery level, trip statistics, engine status, charging state and last
  reported position.
* The trip journal built from that data: trips, places, purposes and exports.
* The owner's login (e-mail address and a hashed password) and the settings they choose.
* Only if the owner turns it on: calendar entries from their own calendar, used to suggest the
  purpose of a trip. For each entry: subject, time, location, free/busy status and whether it
  is marked private. The body text is only stored if the owner enables it.

## Where it is stored

Everything is stored on the owner's own server. Access tokens and other secrets, including a
calendar link (ICS) if the owner has added one, are encrypted.
Nothing is sold, and nothing is used for advertising or profiling.

## What leaves the server

* Requests to the Volvo Cars APIs, to read the owner's own vehicle data.
* Address lookups: a rounded position is sent to OpenStreetMap's Nominatim service to get a
  street address.
* Only when the owner presses "Hämta adressen från kartan" (fetch the address from the map) on
  a saved place: that place's position, rounded to four decimals, is sent once to the same
  service. This is the only way the position of the owner's home can reach it; it is never
  looked up automatically.
* Map tiles: when a map is shown, the browser fetches map images from the configured tile
  server (by default OpenStreetMap). The tile server sees the viewer's IP address, the map area
  and the site's origin (for example `https://drivelog.example/`, never the page path).
* Only if the owner has added a calendar link (ICS): DriveLog fetches that calendar file from
  the calendar provider (for Outlook, Microsoft) every 30 minutes and when the owner asks for
  it. The provider sees the server's address and when the file is fetched. Removing the link
  deletes it and all entries fetched from it.
* Only if the owner turns it on: the minimum information needed is sent to the AI provider the
  owner has chosen, to suggest a category for a trip, and notifications are sent to the owner's
  own Home Assistant.

## Retention and deletion

Data is kept until the owner deletes it. The owner can export the journal, delete history and
disconnect every integration from within DriveLog, and can withdraw the consent with Volvo Cars
at any time.

## Contact

Questions can be raised in the issue tracker of this repository.

See also the [terms and conditions](terms.md).
