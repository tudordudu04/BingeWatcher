import sqlite3;
import typer;
import json;
import os
from typing_extensions import Annotated;
from typing_extensions import Optional;
from typer import Argument;
from typer import Option;
from enum import Enum;
from urllib.parse import urlparse;
from urllib.request import urlopen, Request;
from html.parser import HTMLParser;
from urllib.error import HTTPError, URLError; #don't need rn
from datetime import date
from googleapiclient.discovery import build

app = typer.Typer(
    add_completion=False,
    context_settings={
        "help_option_names": ["-h", "--help"]
    }
)

conn = sqlite3.connect("bingewatcher.db")
conn.execute("PRAGMA foreign_keys = ON")
cursor = conn.cursor()

class Status(str, Enum):
    plan_to_watch = "plan_to_watch"
    watching = "watching"
    on_hold = "on_hold"
    dropped = "dropped"
    watched = "watched"

def init_db():
    schema = """CREATE TABLE IF NOT EXISTS shows(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    status TEXT DEFAULT 'watching' NOT NULL
            CHECK (status IN ('plan_to_watch', 'watching', 'watched', 'dropped', 'on_hold')),
    latest_episode INTEGER DEFAULT 0 NOT NULL,
    last_watched INTEGER DEFAULT 0 NOT NULL,
    rating REAL DEFAULT 0 NOT NULL,
    imdb_link TEXT NOT NULL,
    notify INTEGER DEFAULT 1 NOT NULL,
    last_page_token TEXT
    );
                CREATE TABLE IF NOT EXISTS new_episodes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    plot TEXT,
    rating REAL DEFAULT 0 NOT NULL,
    has_trailer INTEGER DEFAULT 0 NOT NULL,
    has_related_video INTEGER DEFAULT 0 NOT NULL,
    video_link TEXT,
    video_title TEXT,
    FOREIGN KEY (show_id) REFERENCES shows(id) ON DELETE CASCADE
    );
    """
    cursor.executescript(schema)

try:
    init_db()
except sqlite3.Error as e:
    print("Initiation of database error: ", e)

def get_title_id(link: str) -> str:
    schema = urlparse(link)
    if schema.hostname != "www.imdb.com":
        return ""
    
    resource = schema.path.split("/")
    if resource[1] != "title":
        return ""
    
    title_id = resource[2]
    if not title_id:
        return ""
    
    if title_id[:2] != "tt" or not title_id[2:].isnumeric() or not len(title_id[2:]) >= 7:
        return ""
    return resource[2]

#irelevant
class ShowParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.is_show: bool
        self.in_title: bool = False
        self.in_head: bool = False

    def process(self, data: str):
        data = data[:data.find(")")]
        data = data[data.find("(")+1:].rstrip(" \t")
        self.is_show = False if data[0].isdigit() else True
        
    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.in_head = True 
        elif tag == "title":
            self.in_title = True

    def handle_data(self, data):
        if self.in_title and self.in_head:
            self.process(data[0:-1])
        
    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "head":
            self.in_head = False
#irelevant
def parse_show(link: str) -> bool:
    request = Request(
        link,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:145.0) Gecko/20100101 Firefox/145.0"},
        method="GET"
    )

    with urlopen(request) as response:
        html = response.read().decode("utf-8")
    
    parser = ShowParser()
    parser.feed(html)

    return parser.is_show  
#irelevant
def parse_episodes(link: str):
    request = Request(
        link,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:145.0) Gecko/20100101 Firefox/145.0"},
        method="GET"
    )

    with urlopen(request) as response:
        html = response.read().decode("utf-8")

    # parser = EpisodeParser()
    # parser.feed(html)


def is_show(title_id: str) -> bool:
    url = f"https://api.imdbapi.dev/titles/{title_id}"

    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:145.0) Gecko/20100101 Firefox/145.0",
        },
        method="GET",
    )

    with urlopen(req) as response:
        body = json.load(response)

    # body = json.load(body)
    # print(json.loads(body))
    if body["type"] in ["tvSeries", "tvMiniSeries"]:
        return True

    return False

def fetch_page(url: str) -> dict:
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:145.0) Gecko/20100101 Firefox/145.0"
            },
            method="GET",
        )

        with urlopen(req) as response:
            return json.load(response)

def get_episodes(title_id: str) -> list[dict]:
    url_base = f"https://api.imdbapi.dev/titles/{title_id}/episodes?pageSize=50"

    cursor.execute("SELECT last_page_token, latest_episode FROM shows WHERE title_id = ?", (title_id,))
    last_page_token, latest_episode = cursor.fetchone()

    if last_page_token:
        url = f"{url_base}&pageToken={last_page_token}"
        episode_number = latest_episode
    else:
        episode_number = 0
        url = url_base

    data = fetch_page(url)
    episodes = list(data.get("episodes", []))

    page_token = last_page_token if last_page_token else data.get("nextPageToken", "")

    while page_token:
        next_url = f"{url_base}&pageToken={page_token}"
        data = fetch_page(next_url)
        episodes.extend(data.get("episodes", []))
        last_page_token = page_token
        page_token = data.get("nextPageToken", "")

    cursor.execute("UPDATE shows SET last_page_token = ? WHERE title_id = ?", (last_page_token, title_id))
    conn.commit()

    episode_list = []

    for episode in episodes:
        if "releaseDate" not in episode:
            continue

        aux = episode["releaseDate"]
        year = aux.get("year", 1)
        month = aux.get("month", 1)
        day = aux.get("day", 1)
        release_date = date(year, month, day)

        if release_date > date.today():
            break

        if "episodeNumber" not in episode:
            continue
        
        episode_number += 1
        title = episode.get("title", f"Episode {episode_number}")
        plot = episode.get("plot", "")
        rating = episode.get("rating", {}).get("aggregateRating", 0)

        episode_list.append({"nr": episode_number, "title": title, "plot": plot, "rating": rating})

    return episode_list

def set_new_episodes(episode_list: list, show_id: int):
    command = "INSERT INTO new_episodes (show_id, number, title, plot, rating) VALUES (?,?,?,?,?)"
    cursor.execute("SELECT name, last_watched, latest_episode FROM shows WHERE id = ?", (show_id,))
    name, last_watched, latest_episode = cursor.fetchone()
    for episode in episode_list:
        if episode["nr"] > last_watched and episode["nr"] > latest_episode:
            cursor.execute(command, (show_id, episode["nr"], episode["title"], episode["plot"], episode["rating"]))
            get_video_for_episode(episode["nr"], show_id, name)
    
    conn.commit()

def delete_old_episodes(last_watched: int, show_id: int):
    cursor.execute("DELETE FROM new_episodes WHERE show_id = ? AND number <= ?", (show_id, last_watched))
    conn.commit()

def print_episode(show_name, ep):
    video_related = ""
    if ep["has_trailer"]:
        video_related = f"has trailer on YouTube at: {ep["video_link"]}"
    elif ep["has_related_video"]:
        video_related = f"has related YouTube video at: {ep["video_link"]}"
    else:
        video_related = "has no trailers or related videos on YouTube"

    print(
        f"[{show_name}] Ep {ep['number']}: {ep['title']} "
        f"(show status = {ep['status']}, rating = {ep['rating']}, "
        f"{video_related})"
    )

def print_show(show):
    print(
        f"Series name: {show[2]}, status: {show[3]}, latest episode: {show[4]}, "
        f"last episode watched: {show[5]}, your rating: {show[6]}, "
        f"notifications: {'ON' if show[8] == True else 'OFF'}"
    )

def get_api_key() -> bool:
    env_key = os.getenv("YOUTUBE_API_KEY")
    if env_key:
        return env_key

    raise typer.Exit("YOUTUBE_API_KEY not found in environment.")

def get_youtube_videos(query, nr_of_videos) -> list:
    youtube = build(
        "youtube",
        "v3",
        developerKey=get_api_key(),
    )

    request = youtube.search().list(
        part = "snippet",
        maxResults = nr_of_videos,
        q = query,
        type = "video",
        videoDuration = "short"
    )
    response = request.execute()

    return response.get("items", [])

def get_video_for_episode(episode_nr, show_id, show_name: str):
    
    query = f"{show_name} Episode {episode_nr} Trailer"
    results = get_youtube_videos(query, 5)

    if not results:
        return

    set_clause = ""
    for video in results:
        video_title: str = video["snippet"]["title"]
        title_words = video_title.lower().split()

        if {show_name.lower(), "episode", str(episode_nr)}.issubset(title_words):
            video_id = video["id"]["videoId"]
            video_link = f"https://www.youtube.com/watch?v={video_id}"
            if "trailer" in title_words or "sneak" in title_words and "peek" in title_words:
                set_clause = f"has_trailer = '1', video_link = '{video_link}', video_title = '{video_title}'"
                break
            else:
                set_clause = f"has_related_video = '1', video_link = '{video_link}', video_title = '{video_title}'"
                break

    if set_clause:
        command = f"UPDATE new_episodes SET {set_clause} WHERE number = {episode_nr} AND show_id = {show_id}"
        cursor.execute(command)
        conn.commit()

def refresh():
    command = "SELECT id, title_id FROM shows WHERE notify = 1"
    cursor.execute(command)

    rows = cursor.fetchall()

    for (show_id, title_id) in rows:
        episode_list = get_episodes(title_id)
        set_new_episodes(episode_list, show_id)

#
@app.command(help = "Add tv shows into your local storage")
def add(
    name: Annotated[str, Argument(help = "Name of the show.")], 
    imdb_link: Annotated[str, Argument(help = "Link to the IMDb page for the show.")], 
    status: Annotated[Status, Option("--status", "-s", help = "Watching status of the show.")] = "watching", 
    last_watched: Annotated[int, Option("--last-watched", "-l", help = "Number of the last watched episode.")] = None,
    rating: Annotated[float, Option("--rating", "-r", help = "Rating for the show between 1 and 10.")] = 0, 
    notify: Annotated[bool, Option(" /--notify", " /-n", help = "Flag for if you DON'T want to be notified of new content.")] = True
):    
    command = "INSERT INTO shows (title_id, name, imdb_link, status, latest_episode, last_watched, rating, notify) VALUES (?,?,?,?,?,?,?,?)"
    
    title_id = get_title_id(imdb_link) 

    if title_id == "":
        raise typer.Exit("Invalid IMDb link for show.")

    if not is_show(title_id):
        raise typer.Exit("Not a show.")

    cursor.execute(command, (title_id, name, imdb_link, status, 0, last_watched, rating, notify))
    cursor.execute("SELECT id FROM shows WHERE name = ?", (name,))
    show_id = cursor.fetchone()[0]
    conn.commit()

    episode_list = get_episodes(title_id)

    if last_watched == None:
        if status == "watched":
            last_watched = len(episode_list)
        else:
            last_watched = 0

    if notify and len(episode_list) != last_watched:
        set_new_episodes(episode_list, show_id)

    cursor.execute(f"UPDATE shows SET latest_episode = ? WHERE name = ?", (len(episode_list), name))
    conn.commit()

@app.command(help = "Update information about shows")
def update(
    name: Annotated[str, Argument(help = "Name of show you want to update.")],
    new_name: Annotated[str, Option("--new-name", "-n", help = "Update name of show to new_name.")] = None,
    last_watched: Annotated[int, Option("--last-watched", "-l", help = "Update number of the last watched episode.")] = None,
    rating: Annotated[float, Option("--rating", "-r", help = "Update the rating of show.")] = None,
    notify: Annotated[int, Option("--notify", "-t", help = "Update notification status for show.")] = None,
    status: Annotated[Status, Option("--status", "-s", help = "Update watching status for show.")] = None
):

    updates = {}
    if new_name:
        updates["name"] = new_name
    if last_watched:
        updates["last_watched"] = str(last_watched)
    if rating:
        updates["rating"] = str(rating)
    if notify in (0,1):
        updates["notify"] = str(int(notify))
    elif status:
        if status.name == "plan_to_watch" or status.name == "watching":
            updates["notify"] = "1"
        else:
            updates["notify"] = "0"
    if status:
        updates["status"] = status.name

    if not updates:
        return

    cursor.execute("SELECT id, latest_episode FROM shows WHERE name = ?", (name,))
    aux = cursor.fetchone()
    if aux is None:
        raise typer.Exit(f"No show found with name '{name}'.")
    
    show_id = aux[0]
    latest_episode = aux[1]
    
    if last_watched == None and status == "watched":
            updates["last_watched"] = latest_episode 

    set_clause = ", ".join(f"{col} = ?" for col in updates.keys())
    command = f"UPDATE shows SET {set_clause} WHERE name = ?"

    params = list(updates.values()) + [name]
    cursor.execute(command, params)
    conn.commit()
    
    if last_watched:
        delete_old_episodes(last_watched, show_id)

@app.command(help = "Delete one show from storage")
def delete(name: Annotated[str, Argument(help = "Name of show you want to delete.")]):
    # delete = typer.confirm(f"Are you sure you want to delete {name}?")
    if not delete:
        raise typer.Exit("Delete canceled.")
    command = "DELETE FROM shows WHERE name = ?"
    cursor.execute(command, (name,))
    
    conn.commit()
    print("Deleted succesfully.")

@app.command(help = "Command for listing shows")
def catalog(
    sort_by_date: Annotated[bool, Option("--date", "-d", help = "Sort shows by date")] = False,
    sort_by_rating: Annotated[bool, Option("--rating", "-r", help="Sort shows by rating")] = False,
    sort_by_name: Annotated[bool, Option("--name", "-n", help = "Sort shows by name")] = False,
    group_by_status: Annotated[bool, Option("--group-watch", "-w", help="Group by watching status")] = False,
    filter_by_status: Annotated[Optional[list[Status]], Option("--filter", "-f", help="Filter by status")] = None,
):
    where_clause = ""
    if filter_by_status:
        statuses = [s.value for s in filter_by_status]
        placeholders = ", ".join("?" for _ in statuses)
        where_clause = f" WHERE status IN ({placeholders})"

    command = f"SELECT * FROM shows{where_clause}"
    cursor.execute(command)
    shows = cursor.fetchall()
    
    sort_key = sum(bool(key) for key in [sort_by_date, sort_by_name, sort_by_rating])

    if sort_key > 1:
        raise typer.Exit("Enter only one of the sorting flags --date, --rating or --name.")

    if sort_by_rating:
        shows = sorted(shows, key = lambda x: x[6])
    elif sort_by_name:
        shows = sorted(shows, key = lambda x: x[2])
    else:
        shows = sorted(shows, key = lambda x: x[0])

    if group_by_status:
        statuses = ["watched", "dropped", "on_hold", "plan_to_watch", "watching"]
        
        shows_by_status = []
        for show in shows:
            shows_by_status[show[3]].append(show)
        
        for status in statuses:
            if status not in shows_by_status:
                continue

            print(f"For {status}")

            for show in shows_by_status:
                print_show(show)
        return
    

    for show in shows:
        print_show(show)

@app.command(help = "Flips the notify flag for a show.")
def notify(name: Annotated[str, Argument(help = "Name of the show you want to change the notify flag for.")]):
    cursor.execute("SELECT notify FROM shows WHERE name = ?", (name,))
    notify = cursor.fetchone()[0]
    notify = 0 if notify else 1

    cursor.execute("UPDATE shows SET notify = ? WHERE name = ?", (notify, name))
    conn.commit()

@app.command("list", help="Command for listing new episodes")
def list_cmd(
    sort_by_rating: Annotated[bool, Option("--rating", "-r", help="Sort by rating")] = False,
    sort_by_title: Annotated[bool, Option("--title", "-t", help="Sort by title alphabetically")] = False,
    sort_by_date: Annotated[bool, Option("--date", "-d", help="(Not implemented) Sort by date")] = False,
    group_by_show: Annotated[bool, Option("--group-show", "-s", help="Group episodes by show")] = False,
    group_by_status: Annotated[bool, Option("--group-watch", "-w", help="Group by watching status")] = False,
    filter_by_status: Annotated[Optional[list[Status]], Option("--filter", "-f", help="Filter by status")] = None,
):
    sort_flags = [sort_by_rating, sort_by_title, sort_by_date]
    if sum(bool(f) for f in sort_flags) > 1:
        raise typer.Exit("Please use only one of the sorting flags: --rating, --title, or --date.")

    if sort_by_rating:
        sort_key = "rating"
    elif sort_by_title:
        sort_key = "title"
    else:
        sort_key = "number"

    where_clauses = ["notify = 1"]
    params = []

    if filter_by_status:
        statuses = [s.value for s in filter_by_status]
        placeholders = ", ".join("?" for _ in statuses)
        where_clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)

    where_sql = " AND ".join(where_clauses)

    cursor.execute(f"SELECT * FROM shows WHERE {where_sql}", params)
    shows = cursor.fetchall()

    if not shows:
        raise typer.Exit("No shows match the given filters.")

    show_ids = [str(show[0]) for show in shows]
    show_statuses = {show[0]: show[3] for show in shows}

    placeholders = ", ".join("?" for _ in show_ids)
    cursor.execute(f"SELECT * FROM new_episodes WHERE show_id IN ({placeholders})", show_ids)
    new_episodes = cursor.fetchall()

    if not new_episodes:
        raise typer.Exit("No new episodes found for the selected shows.")

    def episode_dict(row):
        return {
            "id": row[0],
            "show_id": row[1],
            "number": row[2],
            "title": row[3],
            "plot": row[4],
            "rating": row[5],
            "has_trailer": row[6],
            "has_related_video": row[7],
            "video_link": row[8],
            "video_title": row[9],
            "status": show_statuses[row[1]],
        }

    episodes = [episode_dict(ep) for ep in new_episodes]

    def sort_key_fn(ep):
        if sort_key == "rating":
            return ep["rating"]
        elif sort_key == "title":
            return ep["title"]
        else:
            return ep["number"]

    if not group_by_show and not group_by_status:
        episodes.sort(key=sort_key_fn)
        for ep in episodes:
            show_name = next(s[2] for s in shows if s[0] == ep["show_id"])
            print_episode(show_name, ep)
        return

    if group_by_show and not group_by_status:
        episodes_by_show: dict[int, list[dict]] = {}
        for ep in episodes:
            episodes_by_show.setdefault(ep["show_id"], []).append(ep)

        for show_id, eps in episodes_by_show.items():
            show_name = next(s[2] for s in shows if s[0] == show_id)
            print(f"For {show_name}:")
            eps.sort(key=sort_key_fn)
            show_name = next(s[2] for s in shows if s[0] == ep["show_id"])
            for ep in eps:
                print_episode(show_name, ep)
            print()
        return

    if group_by_status and not group_by_show:
        episodes_by_status: dict[str, list[dict]] = {}
        for ep in episodes:
            episodes_by_status.setdefault(ep["status"], [])
            episodes_by_status[ep["status"]].append(ep)

        status_order = ["watched", "dropped", "on_hold", "plan_to_watch", "watching"]

        for status in status_order:
            if status not in episodes_by_status:
                continue

            print(f"Status: {status}")
            eps = episodes_by_status[status]
            eps.sort(key=sort_key_fn)
            show_name = next(s[2] for s in shows if s[0] == ep["show_id"])
            for ep in eps:
                print_episode(show_name, ep)
            print()
        return

    episodes_by_show_and_status: dict[tuple[int, str], list[dict]] = {}
    for ep in episodes:
        episodes_by_show_and_status.setdefault((ep["show_id"], ep["status"]), []).append(ep)

    status_order = ["watched", "dropped", "on_hold", "plan_to_watch", "watching"]

    for status in status_order:
        for (show_id, status_key), eps in episodes_by_show_and_status.items():
            if status_key != status:
                continue

            show_name = next(s[2] for s in shows if s[0] == show_id)
            print(f"For {show_name} (status = {status}):")

            eps.sort(key=sort_key_fn)
            for ep in eps:
                print_episode(show_name, ep)
            print()
    return

@app.command("listed", help = "List all new_episodes")
def listed():
    cursor.execute("SELECT * FROM new_episodes")
    for episode in cursor.fetchall():
        print(episode)

@app.command(help = "Seed the database with some shows")
def seed():
    # name link status last_watched rating notify
    # add("Breakings Bad", "https://www.imdb.com/title/tt0903747/", "on_hold", 3, 8)
    # add("Planet Earth II", "https://www.imdb.com/title/tt5491994/", "dropped", 1, 0, 1)
    # add("Planet Earth", "https://www.imdb.com/title/tt0795176/", "dropped", 1, 0, 0)
    # add("Band of Brothers", "https://www.imdb.com/title/tt0185906/", "watched")
    # add("Chernobyl", "https://www.imdb.com/title/tt7366338/", "plan_to_watch", None, 0, 0)
    # add("The Wire", "https://www.imdb.com/title/tt0306414/", "watching", 5, 6, 0)
    # add("Avatar: The Last Airbender", "https://www.imdb.com/title/tt0417299/", "watched", None, 10, 0)
    # add("Pluribus", "https://www.imdb.com/title/tt22202452", "plan_to_watch", None, 0)
    # add("The Sopranos", "https://www.imdb.com/title/tt0141842/?ref_=chttvtp_t_8")
    # add("Blue Planet II", "https://www.imdb.com/title/tt6769208/?ref_=chttvtp_t_9", "dropped", 1, 0, 0)
    # add("Cosmos: A Spacetime Oddysey", "https://www.imdb.com/title/tt2395695/", "dropped", "4", 2)
    # add("Cosmos", "https://www.imdb.com/title/tt0081846/", "watching", None, 0, 0)
    # add("Our Planet", "https://www.imdb.com/title/tt9253866/", "dropped", 4)
    # add("Game of Thrones", "https://www.imdb.com/title/tt0944947/", "plan_to_watch", None, 0, 0)
    # add("Bluey", "https://www.imdb.com/title/tt7678620/", "on_hold", 2, 7)
    # add("The World at War", "https://www.imdb.com/title/tt0071075/", "plan_to_watch", None, 0, 0)
    # add("FMA", "https://www.imdb.com/title/tt1355642/", "watched", None, 10)
    # add("Attack on Mid", "https://www.imdb.com/title/tt2560140/", "watched", None, 7)
    # add("Goat x Goat", "https://www.imdb.com/title/tt2098220/", "watching", 130, 10)
    # add("Cowboy Bebop", "https://www.imdb.com/title/tt0213338/", "plan_to_watch", None, 0, 0)
    # add("Mid Piece", "https://www.imdb.com/title/tt0388629/", "on_hold", 1000, 6, 1)
    # add("Bojack", "https://www.imdb.com/title/tt3398228/", "watching", 14, 9)
    # add("DBZ", "https://www.imdb.com/title/tt0121220/", "plan_to_watch", None, 0, 0)
    # add("Invincible", "https://www.imdb.com/title/tt6741278/", "watched")
    # add("Breakings Bad", "https://www.imdb.com/title/tt0903747/", "watching", 44, 8)
    # add("Invincible", "https://www.imdb.com/title/tt6741278/", "watching", 10)
    # add("Goat x Goat", "https://www.imdb.com/title/tt2098220/", "watching", 46, 10)
    # add("Cowboy Bebop", "https://www.imdb.com/title/tt0213338/", "plan_to_watch", 20, 0, 0)
    add("Pluribus", "https://www.imdb.com/title/tt22202452", "plan_to_watch", 6, 0)


@app.command("del", help = "Delete the whole database of shows")
def dele():
    cursor.execute("DROP TABLE shows")
    cursor.execute("DROP TABLE new_episodes")
    
@app.command("web", help = "test")
def cev(name: Annotated[str, Argument(help = "Name of show you want to search")]):
    print("cev")

refresh()
app()

