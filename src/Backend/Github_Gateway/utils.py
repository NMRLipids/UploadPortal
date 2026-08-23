# app.py
import os, yaml, time
import requests 
from github import Github,GithubIntegration, Auth
from github.Repository import Repository
from flask import current_app as app 
import logging
from datetime import datetime, timedelta,timezone
from typing import Dict
#Constants:
# Base URL for Databank API which is reference to running container
databank_api_url = os.getenv("DATABANK_API_URL", "http://databank_api:8000")

WORK_REPO_NAME = os.getenv('WORK_REPO_NAME') #Where data is originally uploaded
WORK_BASE_BRANCH = 'main' # A branch will be created based on this branch
PULL_REQUEST_TARGET_REPO = os.getenv('PULL_REQUEST_TARGET_REPO')
HELPER_APP_ID      = int(os.getenv("HELPER_APP_ID"))
HELPER_PRIVATE_KEY= os.getenv("HELPER_PRIVATE_KEY")
ADMIN_APP_ID = int(os.getenv("ADMIN_APP_ID"))
ADMIN_PRIVATE_KEY = os.getenv("ADMIN_PRIVATE_KEY")

helper_integration = GithubIntegration(auth=Auth.AppAuth(app_id=HELPER_APP_ID,private_key=HELPER_PRIVATE_KEY))
admin_integration = GithubIntegration(auth=Auth.AppAuth(app_id=ADMIN_APP_ID,private_key=ADMIN_PRIVATE_KEY))



logger = logging.getLogger('gunicorn.error')

#Dictionary of repositories and their github apps
integration_map =  {
    WORK_REPO_NAME: admin_integration,
    PULL_REQUEST_TARGET_REPO: helper_integration
}


#Caching the tokens which are valid for 1 hour
token_cache = {
    WORK_REPO_NAME: {
        "token":   None,
        "expires": datetime.min.replace(tzinfo=timezone.utc),
        "client":  None,
        "repo":    None
    },
    PULL_REQUEST_TARGET_REPO: {
        "token":   None,
        "expires": datetime.min.replace(tzinfo=timezone.utc),
        "client":  None,
        "repo":    None
    },
}

def get_integration(repo_full_name:str) -> GithubIntegration:
    """" Return integration for the given full name of repository name. e.g NMRLipids/UserData"""
    if repo_full_name in integration_map:
        return integration_map[repo_full_name]
    else: raise ValueError(F"Unknown repository for integration: {repo_full_name}")



def get_github_client_and_repo(repo_full_name: str):
    """
    Returns (client, repo) for repo_full_name, caching both until just
    before the token expires (5-minute buffer). Mints a fresh token 
    and repo object only when needed.
    """
    cache = token_cache[repo_full_name]
    now   = datetime.now(timezone.utc)
    
    if cache["token"] is None or now + timedelta(minutes=5) >= cache["expires"]:
        owner, repo = repo_full_name.split("/", 1)
        integration = get_integration(repo_full_name)
        installation = integration.get_repo_installation(owner, repo)
        tok          = integration.get_access_token(installation.id)
        
        client = Github(tok.token)
        repo_obj = client.get_repo(repo_full_name)
        
        cache.update({
            "token":   tok.token,
            "expires": tok.expires_at,
            "client":  client,
            "repo":    repo_obj
        })
        logger.info(f"Refreshed token for {repo_full_name}")
    else:
        logger.info(f"Reusing cached client for {repo_full_name}. Remaining lifetime: {cache['expires']-now}")

    return cache["client"], cache["repo"]



def get_gh_work() -> Github:
    "Returns a GitHub client for the work repo"
    client, _ = get_github_client_and_repo(WORK_REPO_NAME)
    return client

def get_repo_work() -> Repository:
    "Return Github repo object for the work repo"
    _, repo = get_github_client_and_repo(WORK_REPO_NAME)
    return repo

def get_gh_target() -> Github:
    "Returns a GitHub client for the target repo"
    client, _ = get_github_client_and_repo(PULL_REQUEST_TARGET_REPO)
    return client

def get_repo_target() -> Repository:
    "Return Github repo object for the work repo"
    _, repo = get_github_client_and_repo(PULL_REQUEST_TARGET_REPO)
    return repo


def is_input_invalid(info_yaml_dict: dict):
    """
    Validates the provided YAML dict via the Databank API.
    Returns None if valid, otherwise the error(s)
    """
    logger.info(f"Validating info yml")
    resp = requests.post(
        f"{databank_api_url}/info-valid-check", json=info_yaml_dict,timeout=10
    )
    if resp.status_code == 200:
        return None
    try:
        errors = resp.json().get("error", resp.text)
    except ValueError:
        errors = resp.text
    logger.error(f"Validation failed: {errors}")

    return errors;  


def sync_upstream():
    """ Pulls latest changes from upstream into work repo"""
    try:
        status, headers, body = get_gh_work()._Github__requester.requestJson(
            "POST",
            f"/repos/{WORK_REPO_NAME}/merge-upstream",
            input={"branch": WORK_BASE_BRANCH}
        )
        logger.info(f"Sync upstream status: {status}")
        if (200 <= status < 300):
            logger.info(f"Successfully synced work-repo against upstream")
        else:
            msg = f"Failed to sync upstream: code={status}, error={body}"
            logger.error(msg)
    except Exception as e:
        logger.error(f"Failed to sync upstream for {WORK_REPO_NAME}: {e}")
        raise


def branch_out(base_branch: str = WORK_BASE_BRANCH) -> str:
    """
    Creates and returns a new timestamped branch off based off WORK_BASE_BRANCH.
    """
    ts         = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    new_branch = f"bot/info_yaml_{ts}"
    repo_work = get_repo_work()
    try: 
        sha = repo_work.get_branch(base_branch).commit.sha
        repo_work.create_git_ref(ref=f"refs/heads/{new_branch}", sha=sha)
        logger.info(f"Created branch {new_branch}")
    except Exception as e:
        logger.error(f"Failed to create branch, error: {e}")
        raise 

    return new_branch

def push_info_file(data: dict, username: str) -> str:
    logger.info(f"Pushing to repository with data from {username}")
    sync_upstream()
    new_branch = branch_out(WORK_BASE_BRANCH)
    yaml_text  = yaml.safe_dump(data, sort_keys=False, width=120)
    path       = f"UserData/info.yml"
    message    = f"Add info.yml from {username}"

    get_repo_work().create_file(
        path=path,
        message=message,
        content=yaml_text,
        branch=new_branch
    )
    return new_branch

def create_pull_request_to_target(
    head_branch: str,
    title: str,
    body: str = "",
    head_repo: str = WORK_REPO_NAME,   
) -> str:
    """
    Create PR from head_repo:head_branch into PULL_REQUEST_TARGET_REPO:WORK_BASE_BRANCH.
    Works for same-org fork -> upstream.
    """
    repo = get_repo_target()  
    payload = {
        "title": title,
        "body": body,
        "base": 'main',       
        "head": head_branch,           
        "head_repo": head_repo,       
        "maintainer_can_modify": False,
        "labels": ["contribute:sim"],
    }

    _, data = repo._requester.requestJsonAndCheck("POST", f"{repo.url}/pulls", input=payload)
    return data["html_url"]


def user_has_push_access(user_token: str) -> bool:
    """
    Given a user's OAuth token, verify they have write/admin rights
    on the pull request target repo by checking collaborator permissions.
    """
    #Get username from their token
    try:
        gh_user = Github(user_token)
        username = gh_user.get_user().login
    except Exception as e:
        logger.warning(f"Failed to retrieve GitHub user: {e}")
        return False

    #Use GitHub to check permission
    try:
        repo    = get_repo_target()
        logger.info(f"Repository name for permission check: {repo} ")
        perm    = repo.get_collaborator_permission(username)  # "read","write","admin","none"
        logger.info(f"User {username} has permissions: {perm}")
    except Exception as e:
        logger.warning(f"Permission check failed for user {username}: {e}")
        return False

    return perm in ("write", "admin")
