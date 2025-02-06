all:	help

create_campaign: 	# create campaign
	uv run linkmonk_rss.py

clean:		# delete last_update json
	rm last_update.json
