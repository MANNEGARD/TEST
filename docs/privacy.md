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
* Only if the owner uploads one: a company logo (PNG or JPEG) that is placed in the exported
  trip journal.
* Only if the owner connects a calendar: entries from their own calendar, used to suggest the
  purpose of a trip. For each entry: subject, time, location, free/busy status and whether it
  is marked private. From version 1.3.0 the whole entry is read by default – the description
  (up to 4,000 characters) and the attendees (name, e-mail address, organizer or attendee) as
  far as the shared calendar contains them. The owner can turn this off under Settings →
  Calendar ("Läs hela bokningen"). The owner's work e-mail domain is guessed from the entries
  (or entered by the owner) to tell colleagues from external attendees.

## Where it is stored

Everything is stored on the owner's own server. Access tokens and other secrets, including a
calendar link (ICS) if the owner has added one, are encrypted.
Nothing is sold, and nothing is used for advertising or profiling.

## What leaves the server

* Requests to the Volvo Cars APIs, to read the owner's own vehicle data.
* Address lookups: the position of a new stop (six decimals) is sent to OpenStreetMap's
  Nominatim service to get a street address. From version 1.3.0 this also applies to the
  owner's confirmed home, which is looked up automatically once. Places that only have an
  address (for example from an imported journal) are sent as address text to the same service
  to get a position.
* When the owner presses "Hämta adressen från kartan" (fetch the address from the map) on a
  saved place: that place's position is sent once to the same service.
* Map tiles: when a map is shown, the browser fetches map images from the configured tile
  server (by default OpenStreetMap). The tile server sees the viewer's IP address, the map area
  and the site's origin (for example `https://drivelog.example/`, never the page path).
* Only if the owner has added a calendar link (ICS): DriveLog fetches that calendar file from
  the calendar provider (for Outlook, Microsoft) every 30 minutes and when the owner asks for
  it. The provider sees the server's address and when the file is fetched. Removing the link
  deletes it and all entries fetched from it.
* Only if the owner has chosen an AI provider (Anthropic or OpenAI) and added an API key:
  information about a trip is sent to that provider to suggest a category and a written
  purpose. The owner chooses how much under Settings → AI suggestions:
  * **Full context (default from version 1.3.0).** Everything that helps the assessment:
    names and street addresses of the start, destination and stops (including the owner's home
    and private places), the map's name and type for unknown places, place notes, all trips the
    same day with times, places, categories and confirmed purposes, the trip before and after,
    the history for the same destination and route, the owner's own rules, the calendar entries
    for the day – and for trips to another town the whole stay and the next days – with subject,
    location, a cleaned description and the attendees' names marked as colleague or external
    (with the external e-mail domain as a hint), the employer's name and the owner's own
    description of their work. Entries marked private are sent only as a time ("Privat
    bokning"), without subject, location, description or attendees.
  * **Minimal.** The place's name or town, weekday, time, distance, the rule engine's signals
    and earlier confirmed purposes. Home and private places are sent as "Home" and "Private
    place". The subject, time and location of calendar entries that match the trip are sent
    unless the owner turns that off (it is on by default). The first 300 characters of an
    entry's description and street addresses are only sent if the owner turns each of them on.
    At this level the subject and the description are sent as they are written in the calendar:
    codes, links, e-mail addresses or passwords that someone has written there are not removed.
  * At both levels, never: coordinates, the vehicle's VIN or registration number, DriveLog's own
    API keys and tokens, the owner's passwords, the calendar link, the Home Assistant token or
    the attendees' e-mail addresses. At the full level, links, e-mail addresses, codes, meeting
    IDs and passwords found in subjects, locations, descriptions and notes are also removed
    before sending. The provider processes the data under its own terms; DriveLog keeps
    nothing at the provider.
* Notifications are sent to the owner's own Home Assistant when the owner has connected it.
  From version 1.3.0 the questions about new trips show the full street address with house
  number (postcode removed) for places without a confirmed name, and the owner's home with its
  address; private places are shown with their name or address. The texts pass through the
  owner's Home Assistant to the owner's phone. The owner can hide all places on the lock screen
  ("Dölj platser på låsskärmen"). Alerts, month notices and the notice about a new version
  never contain places. A notice that DriveLog has been updated contains only the version
  numbers and the main news from the change log.
* A diagnostics file ("felsökningsfil") with the vehicle's recent readings, fetch results and
  trips is created only when the owner downloads it, and leaves the server only if the owner
  sends it on. It contains no VIN, registration number, positions, addresses, place names,
  raw Volvo data, error texts or secrets.
* Fuel and energy use per trip is calculated on the server from the vehicle's own readings;
  nothing extra is fetched or sent for it.

## Retention and deletion

Data is kept until the owner deletes it. The owner can export the journal, delete history and
disconnect every integration from within DriveLog, and can withdraw the consent with Volvo Cars
at any time.

## Contact

Questions can be raised in the issue tracker of this repository.

See also the [terms and conditions](terms.md).
