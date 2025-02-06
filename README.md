# Linkmonk RSS

## About

This is an extension to Linkmonk to send out campaigns for RSS updates. 

## Requirements

- A running and publicly available Linkmonk instance (for example deployed with
  PikaPods).
- A GitHub accountd and a fork of this repository.
- An RSS feed to consume. 

## Technical details

This is a Python script that takes advantage of [Linkmonk's
API](https://listmonk.app/docs/apis/apis) to create a campaign This is [similar
to this Python
script](https://github.com/ElliotKillick/rss2newsletter/blob/main/rss2newsletter),
but written from scratch.

The parameters will be in a `.env` that is read by `python-dotenv`. 
We use a Jinja2 template file for the template of the campaign newsletter.

You can run this python script locally on your machine and deploy it with a
GitHub action. 

To persist the state of the last date of the RSS feed update, a JSON file with
a timestamp is stored. If it is run on GitHub, we use actions/upload-artifact
and actions/download-artifact to store the state.

The Python library management is done with `uv` using `pyproject.toml`. We run
the script with `uv run linkmonk_rss.py`. The script uses a CLI interface where
we can also do a dry-run.

```
LINKMONK_API=YOURSECRETAPIKEY
LINKMONK_HOST=https://yourlinkmonkhost.com
LIST_NAME=<The name of the list in LinkMonk>
RSS_FEED=https://blog.heuel.org/feeds/all.atom.xml
```

