import argparse
import datetime
import json
import time
from typing import List

import requests
import requests.auth
from httplib2 import Response
from oauth2client import tools

import gcalendar
import project_settings
from apikeys import GIST_API_KEY, GIST_FILE_ID
from common.match import Match
from livescore_in import LiveScoreDownloader

today = datetime.datetime.now().strftime("%Y-%m-%d")


def create_event(service, calendar_id: str, match: Match):
    # create the default duration for the tennis match
    match_time_end = match.time.start + datetime.timedelta(minutes=project_settings.MATCH_DEFAULT_DURATION_MINUTES)
    now = datetime.datetime.now(tz=datetime.timezone.utc)

    # if the tennis match is still going on, but there is no existing event,
    # then the computed end time might already have passed
    # e.g. the match has been going on 1h 40 mins, but the default duration is 1h 30 mins.
    # The computed end time will show that the match has ended 10 minutes ago, which is wrong.
    # This checks for that case and makes sure that
    # if the initial match end time is less than NOW but the match is still GOIGN ON, then the end time is extended
    if match.is_still_going() and match_time_end < now:
        match_time_end = now + datetime.timedelta(minutes=project_settings.MATCH_EXTEND_MINUTES)

    event = {
        "summary": match.name,
        "start": {
            "dateTime": match.time.start.isoformat()
        },
        "end": {
            "dateTime": match_time_end.isoformat()
        },
        "maxAttendees": "100",
        "guestsCanInviteOthers": "true",
        "guestsCanSeeOtherGuests": "true",
        "anyoneCanAddSelf": "true",
        "colorId": match.color
    }
    created_event = service.events().insert(calendarId=calendar_id, body=event).execute()
    print("Created event.")


def different_start_times(old, new):
    # the -1 on the existing event returns the datetime string without the Z at the end
    # the rfind on match_time_start returns the datetime string woithout the +00:00 a the end
    # they remove the 2 ways to specify timezone in the standard ISO format
    # the reason we can remove it is that both are guaranteed to be UTC
    return old["start"]["dateTime"][:-1] != new[:new.rfind("+")]


def different_end_times(old, new):
    # the -1 on the existing event returns the datetime string without the Z at the end
    # the rfind on match_time_start returns the datetime string woithout the +00:00 a the end
    # they remove the 2 ways to specify timezone in the standard ISO format
    # the reason we can remove it is that both are guaranteed to be UTC
    return old["end"]["dateTime"][:-1] != new[:new.rfind("+")]


def different_colors(old, new):
    return old["colorId"] != new


def show_if_different(word: str, old: str, new: str, time=False) -> str:
    if old != new:
        full_str = f"\t\told {word}: {old}\n\t\tnew {word}: {new}\n"

        # if the data is times, then parse and show difference
        if time:
            old_dt = from_google_date_to_datetime(old)
            new_dt = from_google_date_to_datetime(new)
            full_str += f"\t\tdiff: {new_dt-old_dt}\n"

        return full_str
    else:
        return ""


def from_google_date_to_datetime(google_date: str) -> datetime.datetime:
    return datetime.datetime.strptime(google_date.replace("Z", "+0000"),
                                      "%Y-%m-%dT%H:%M:%S%z")


def from_google_date_to_datetime_ms(google_date: str) -> datetime.datetime:
    """
    Includes microseconds in the date format
    :param google_date: String containing the date
    :return: parsed datetime object
    """
    return datetime.datetime.strptime(google_date.replace("Z", "+0000"),
                                      "%Y-%m-%dT%H:%M:%S.%f%z")


def update_event(service, calendar_id: str, match: Match, existing_event: {}):
    match_time_start = match.time.start.isoformat()
    event_time_end = from_google_date_to_datetime(existing_event["end"]["dateTime"])

    # If the event's end is less than the default match duration, then the end will be fixed
    # this can happen if the match time is moved N minutes forward, the end time also needs to be adjusted
    # NOTE this will only adjust the end time FORWARD. If the match is moved backwards the end time will NOT be
    # changed. Adjusting the time when the match is moved backwards will cause an issue with the event end adjustment!
    expected_duration = match.time.start + datetime.timedelta(minutes=project_settings.MATCH_DEFAULT_DURATION_MINUTES)
    if event_time_end < expected_duration:
        print("\tAdjusting end time.")
        event_time_end = expected_duration

    # if the match is live, but the end of the event in the calendar has passed, extend the end of the event
    # this will show in the calendar that the match hasn't yet ended
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    if match.is_still_going() and event_time_end < now:
        # move the end forward
        print("\tExtending end as it is still going.")
        event_time_end = now + datetime.timedelta(minutes=project_settings.MATCH_EXTEND_MINUTES)

    event_time_end = event_time_end.isoformat()

    if different_start_times(existing_event, match_time_start) \
            or different_end_times(existing_event, event_time_end) \
            or different_colors(existing_event, match.color):

        old_start = existing_event["start"]["dateTime"]
        old_end = existing_event["end"]["dateTime"]
        old_color = existing_event["colorId"]

        existing_event["start"] = {"dateTime": match_time_start}
        existing_event["end"] = {"dateTime": event_time_end}
        existing_event["colorId"] = match.color
        update_result = service.events().update(calendarId=calendar_id, eventId=existing_event["id"],
                                                body=existing_event).execute()

        # the end="" removes the new line at the end
        print("\tUpdated event:\n",
              show_if_different("start", old_start, update_result["start"]["dateTime"], time=True),
              show_if_different("end", old_end, update_result["end"]["dateTime"], time=True),
              show_if_different("status", str(match.status_from_color(old_color)),
                                str(match.status_from_color(match.color))),
              end="")
    else:
        print("\tNo change.")


def update_finished_event(service, calendar_id: str, match: Match, existing_event: {}):
    event_time_start = from_google_date_to_datetime(existing_event["start"]["dateTime"])
    event_time_end = from_google_date_to_datetime(existing_event["end"]["dateTime"])
    now = datetime.datetime.now(tz=datetime.timezone.utc)

    # if the event has finished, but it's end time is after NOW, then change it to now
    # but make sure the event has actually started first, to avoid a Google Calendar event error
    if event_time_end > now > event_time_start:
        # convert the end time to isoformat at the same time as assigning
        event_time_end = now.isoformat()
        old_end = existing_event["end"]["dateTime"]
        old_color = existing_event["colorId"]

        existing_event["end"] = {"dateTime": event_time_end}
        existing_event["colorId"] = match.color
        update_result = service.events().update(calendarId=calendar_id, eventId=existing_event["id"],
                                                body=existing_event).execute()

        # the end="" removes the new line at the end
        print("\tUpdated finished event:\n",
              show_if_different("end", old_end, update_result["end"]["dateTime"]),
              show_if_different("status", str(match.status_from_color(old_color)),
                                str(match.status_from_color(match.color))),
              end="")
    elif existing_event["colorId"] != match.color:
        old_color = existing_event["colorId"]

        existing_event["colorId"] = match.color
        update_result = service.events().update(calendarId=calendar_id, eventId=existing_event["id"],
                                                body=existing_event).execute()
        print("\tUpdated finished event color:\n",
              show_if_different("status", str(match.status_from_color(old_color)),
                                str(match.status_from_color(match.color))),
              end="")
    else:
        print("\tFinished. No changes.")


def update_calendar_events(service, calendar_id, matches: List[Match]):
    today = datetime.datetime.today()
    # used to query events only for today
    midnight = datetime.datetime(today.year, today.month, today.day, 0, 0)
    # convert to the ISO format string with Z at the end to signify UTC timezone
    midnight = midnight.isoformat() + "Z"

    # get all events for today, hopefully 100 is big enough for a calendar
    # WARNING this also means that if this code is executed for past days, then the events WILL NEVER BE UPDATED
    # as they are never queried and duplicates will be created instead of updating the time!
    events_result = service.events().list(calendarId=calendar_id, maxResults=100, timeMin=midnight).execute()
    events = events_result.get('items', None)
    print("Retrieved events for calendar.")

    # TODO batch request https://developers.google.com/google-apps/calendar/batch
    for match in matches:
        match_event = [event for event in events if event["summary"] == match.name]
        if len(match_event) > 1:
            raise ValueError("There is more than one event with matching names, and there should only be one!")

        print("Match: ", match.name)

        # if no events are found for that match then a new one is created
        if len(match_event) == 0:
            create_event(service, calendar_id, match)
        else:
            match_event = match_event[0]

            if match.is_finished():
                update_finished_event(service, calendar_id, match, match_event)
            else:
                update_event(service, calendar_id, match, match_event)


def create_calendar(service, tournament_name):
    calendar_body = {
        "summary": tournament_name
    }
    print("Creating calendar: ", tournament_name)
    created_calendar = service.calendars().insert(body=calendar_body).execute()
    public_permissions = {
        "role": "reader",
        "scope": {
            "type": "default"
        }
    }

    print("Adding public READ permission.")
    service.acl().insert(calendarId=created_calendar["id"], body=public_permissions).execute()
    return created_calendar["id"]


def append_calendar_to_list(list_object: List[str], calendar_summary: str, calendar_id: str):
    list_object.append(f"""### {calendar_summary}
* ICAL: {project_settings.CALENDAR_ICAL_BASE_URL.format(calendar_id)}
* Embed: {project_settings.CALENDAR_EMBED_BASE_URL.format(calendar_id)}
* IFRAME: {project_settings.CALENDAR_IFRAME_BASE.format(calendar_id)}
<hr/>
""")


def generate_calendar_urls(service) -> Response:
    calendars_list_result = service.calendarList().list().execute()
    calendars_list = calendars_list_result.get('items', None)
    file_data: List[str] = [
        rf"""Updated (UTC) time: {datetime.datetime.utcnow().isoformat()+'Z'}

### Importing a calendar:
1. Copy ICAL link from below.
1. Add calendar by URL
    - Google: Go to [Add by URL](https://calendar.google.com/calendar/b/1/r/settings/addbyurl)
        - Name is read automatically
    - Web Outlook: Go to your [Calendar](https://outlook.live.com/owa/?path=/calendar/view/Day)
        - Near the top of the screen there should be `Add Calendar`
        - After clicking it, there should be `From the Internet`
        - Name will be read automatically, although there is an escape character `\` before each coma
    - Web Outlook BETA: Go to your [Calendar](https://outlook.live.com/calendar/#/view/day/)
        - In the Calendar list on the left there should be `Discover Calendars`
        - At the bottom of the `Discover Calendars` window there should be `From web`
        - Name must be manually added
    - Windows 10 Calendar:
        - Can't add calendar from URL as far as I am aware
    - Office Outlook 2016:
        - Find `Open Calendar` button, should be near to top, it might be folded in a `Manage Calendars` folder
            - Alternatively go to `Folder -> Open Calendar`
        - After clicking it there should be `From Internet`
1. Paste link and click add/import.
<hr/>

### Notes:
- The ICAL will be refreshed whenever your calendar application decides to query for changes. This can differ. On Google it changes are quickly reflected.
- This is still being tested and something will probably fail.

<hr/>

### Calendar Event Colours (in Google)
- Cancelled is Graphite (gray)
- Finished is Grape (purple)
- Not started is Sage (blue-ish green-ish)
- Live/started is Basil (green)
- Interrupted is Lavender

<hr/>
"""
    ]

    # matches that are live
    active: List[str] = []
    # matches that have not had a match in the last day
    inactive: List[str] = []
    # matches that have not had a match in the last 3 days
    archive: List[str] = []
    for calendar in calendars_list:
        # remove primary calendar, and #contacts and #holidays
        if "@gmail" not in calendar["id"] and "#" not in calendar["id"]:
            #             file_data.append(f"""### {calendar["summary"]}
            # * ICAL: {project_settings.CALENDAR_ICAL_BASE_URL.format(calendar["id"])}
            # * Embed: {project_settings.CALENDAR_EMBED_BASE_URL.format(calendar["id"])}
            # * IFRAME: {project_settings.CALENDAR_IFRAME_BASE.format(calendar["id"])}
            # <hr/>
            # """)
            latest_event = service.events().list(calendarId=calendar["id"], maxResults=1).execute()
            t1 = from_google_date_to_datetime_ms(latest_event["updated"])
            time_elapsed = datetime.datetime.now(tz=datetime.timezone.utc) - t1
            if time_elapsed > datetime.timedelta(days=3):
                append_calendar_to_list(archive, calendar["summary"], calendar["id"])
            elif time_elapsed > datetime.timedelta(days=1):
                append_calendar_to_list(inactive, calendar["summary"], calendar["id"])
            else:
                append_calendar_to_list(active, calendar["summary"], calendar["id"])

    file_data.append("# Active")
    file_data.append("\n".join(active))
    file_data.append("# Inactive")
    file_data.append("\n".join(inactive))
    file_data.append("# Archive")
    file_data.append("\n".join(archive))

    file_data: str = "\n".join(file_data)
    with open(project_settings.CALENDAR_URLS_FILENAME, 'w') as f:
        f.write(file_data)

    gist = {
        "description": "Tennis Calendars",
        "public": "true",
        "files": {
            project_settings.CALENDAR_URLS_FILENAME: {
                "content": file_data
            }
        }

    }
    print("Uploading calendar urls to GIST.")
    gist_response = requests.patch(f'https://api.github.com/gists/{GIST_FILE_ID}',
                                   auth=requests.auth.HTTPBasicAuth("DTasev", GIST_API_KEY),
                                   data=json.dumps(gist))
    print(gist_response)
    return gist_response


def update_calendars(service, downloader) -> Response:
    calendars_list_result = service.calendarList().list().execute()
    calendars_list = calendars_list_result.get('items', None)
    calendars = {}
    print("Calendars downloaded.")

    # create a dictionary for every calendar
    for c in calendars_list:
        calendars[c["summary"]] = {"id": c["id"]}

    print("Calendars moved to dictionary.")

    tournaments = downloader.download()

    id = 0
    # for every tournament
    for tournament_name, matches in tournaments.items():
        print("Processing tournament:", tournament_name)
        # check if there is a calendar for the tournament
        if tournament_name not in calendars:
            print("Calendar not found, creating a new one...")
            calendar_id = create_calendar(service, tournament_name)
        else:
            print("Calendar already exists.")
            calendar_id = calendars[tournament_name]["id"]
        update_calendar_events(service, calendar_id, matches)

        # Process every entry from the remote data. This is used to limit how many entries are processed
        # during development. If --no-limit is not specified, then only the first 10 will be processed
        if not args.no_limit:
            if id == 10:
                break
            id += 1

    print("Generating calendar URLs.")
    # pass in the service so that all calendars can be retrieved again.
    # This will include any newly created calendars here
    return generate_calendar_urls(service)


def main(args):
    service = gcalendar.auth(args)
    downloader = LiveScoreDownloader()

    try:
        while True:
            response = update_calendars(service, downloader)

            # TODO count failure and on Nth failure send me an email
            if response.status_code != 200:
                print(f"Expected 200 OK, but got {response}")

            # TODO need a way to purge tournaments with matches older than a week? or longer?
            # or move into a past section in the online MD page

            # TODO make a webpage where u can click + and subscribe to tournament and get emails or phone notifications
            # this should be doable by using Chrome's notification feature (need to check API)

            time.sleep(60)
    except KeyboardInterrupt:
        downloader.quit()


def setup_args() -> argparse.ArgumentParser:
    # add arguments for google authentication
    parser = argparse.ArgumentParser(parents=[tools.argparser])

    # additional arguments for the package
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--no-limit", action="store_true")
    return parser


if __name__ == "__main__":
    parser = setup_args()
    args = parser.parse_args()
    main(args)
