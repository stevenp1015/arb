#!/bin/zsh

# Hey Steven, this is the script to watch your local index.html, securely thrust it 
# into your remote server (tokyo2), and instantly force your browser to reload.

FILE="index.html"
REMOTE="tokyo2:/root/screener/"
PREV_MOD=$(md5 -q "$FILE" 2>/dev/null)

echo "watching $FILE for changes. prepare your asshole for some aggressive syncing."

while true; do
  sleep 0.5
  LAST_MOD=$(md5 -q "$FILE" 2>/dev/null)
  
  if [ "$LAST_MOD" != "$PREV_MOD" ]; then
    echo "change detected. thrusting $FILE deep into $REMOTE."
    
    # send the file to the remote server
    scp "$FILE" "$REMOTE"
    
    if [ $? -eq 0 ]; then
      echo "successfully injected. forcing your browser to take it..."
      
      # safely reload browsers only if they are actually fucking running so we don't launch them
      osascript -e 'if application "Google Chrome" is running then tell application "Google Chrome" to tell the active tab of its first window to reload' 2>/dev/null
      osascript -e 'if application "Brave Browser" is running then tell application "Brave Browser" to tell the active tab of its first window to reload' 2>/dev/null
      osascript -e 'if application "Safari" is running then tell application "Safari" to set URL of document 1 to (URL of document 1)' 2>/dev/null
      osascript -e 'if application "Arc" is running then tell application "Arc" to tell front window to reload' 2>/dev/null
      
    else
      echo "fuck. scp failed. check your ssh connection, idiot."
    fi
  fi
  
  PREV_MOD=$LAST_MOD
done
