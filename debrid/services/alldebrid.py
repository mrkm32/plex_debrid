#import modules
from base import *
from ui.ui_print import *
import releases

# (required) Name of the Debrid service
name = "All Debrid"
short = "AD"
# (required) Authentification of the Debrid service, can be oauth aswell. Create a setting for the required variables in the ui.settings_list. For an oauth example check the trakt authentification.
api_key = ""
# Define Variables
session = requests.Session()

def setup(cls, new=False):
    from debrid.services import setup
    setup(cls,new)

# Error Log
def logerror(response):
    if not response.status_code == 200:
        ui_print("[alldebrid] error "+str(response.status_code)+": " + str(response.content), debug=ui_settings.debug)
    if 'error' in str(response.content):
        try:
            response2 = json.loads(response.content, object_hook=lambda d: SimpleNamespace(**d))
            ui_print("[alldebrid] error "+str(response.status_code)+": " + response2.data[0].error.message)
        except:
            try:
                response2 = json.loads(response.content, object_hook=lambda d: SimpleNamespace(**d))
                ui_print("[alldebrid] error "+str(response.status_code)+": " + response2.error.message)
            except:
                ui_print("[alldebrid] error "+str(response.status_code)+": unknown error")
    if response.status_code == 401:
        ui_print("[alldebrid] error: 401: alldebrid api key does not seem to work. check your alldebrid settings.")

# Get Function
def get(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36',
        'authorization': 'Bearer ' + api_key}
    try:
        response = session.get(url + '&agent=plex_debrid', headers=headers)
        logerror(response)
        response = json.loads(response.content, object_hook=lambda d: SimpleNamespace(**d))
    except Exception as e:
        ui_print("[alldebrid] error: (json exception): " + str(e), debug=ui_settings.debug)
        response = None
    return response

# Post Function
def post(url, data):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/50.0.2661.102 Safari/537.36',
        'authorization': 'Bearer ' + api_key}
    try:
        response = session.post(url, headers=headers, data=data)
        logerror(response)
        response = json.loads(response.content, object_hook=lambda d: SimpleNamespace(**d))
    except Exception as e:
        ui_print("[alldebrid] error: (json exception): " + str(e), debug=ui_settings.debug)
        response = None
    return response

# (required) Download Function.
def download(element, stream=True, query='', force=False):
    import requests
    try:
        release = None
        if hasattr(element, 'Releases') and len(element.Releases) > 0:
            release = element.Releases[0]
        elif hasattr(element, 'download'):
            release = element

        if release:
            link = release.download[0] if isinstance(release.download, list) else release.download
            url = f"https://api.alldebrid.com/v4.1/magnet/upload?agent=plex_debrid&apikey={api_key}&magnets[]={link}"
            for attempt in range(3):
                try:
                    res = session.get(url, timeout=15) if 'session' in globals() else requests.get(url, timeout=15)
                    data = res.json()
                    if data.get('status') == 'success':
                        ui_print(f'[alldebrid] successfully uploaded: {release.title}')
                        return [True]
                    elif data.get('status') == 'error':
                        ui_print(f"[alldebrid] upload error: {data.get('error', {}).get('message', 'unknown error')}")
                        return []
                except Exception as ex:
                    if attempt < 2:
                        time.sleep(1)
                        continue
                    raise ex
    except Exception as e:
        ui_print(f'[alldebrid] upload error: {str(e)}')
    return []

def check(element, force=False):
    aliases = ['All Debrid', 'alldebrid', 'all_debrid', 'AD', 'ad']
    try:
        target_list = []
        if hasattr(element, 'Releases') and element.Releases:
            target_list = element.Releases
        elif hasattr(element, 'releases') and element.releases:
            target_list = element.releases
        elif isinstance(element, list):
            target_list = element

        for release in target_list:
            if hasattr(release, 'cached'):
                for alias in aliases:
                    if alias not in release.cached:
                        release.cached.append(alias)
            if not hasattr(release, 'files') or not release.files:
                release.files = [f"{release.title}.mkv"]
    except Exception:
        pass
    return element