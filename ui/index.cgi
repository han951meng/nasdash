#!/bin/bash

cgi_name="index.cgi"
target_url="http://127.0.0.1:9800";

if [[ "$REQUEST_URI" == *"$cgi_name"* ]]; then
    after_proxy="${REQUEST_URI#*$cgi_name}"
    
    if [[ "$after_proxy" == *"?"* ]]; then
        target_path=$(echo "$after_proxy" | cut -d'?' -f1)
        target_query=$(echo "$after_proxy" | cut -d'?' -f2-)
    else
        target_path="$after_proxy"
        target_query=""
    fi
else
    after_proxy=""
    target_path=""
    target_query="$QUERY_STRING"
fi

if [ -z "$target_path" ]; then
    target_path="/"
fi

target_url="$target_url$target_path"
if [ -n "$target_query" ]; then
    target_url="$target_url?$target_query"
fi
curl_args=(-s --include -X "$REQUEST_METHOD")

if [ -n "$HTTP_COOKIE" ]; then
    curl_args+=(-H "Cookie: $HTTP_COOKIE")
fi
if [ -n "$CONTENT_TYPE" ]; then
    curl_args+=(-H "Content-Type: $CONTENT_TYPE")
fi
curl_args+=("$target_url")

if [ "$REQUEST_METHOD" = "POST" ]; then
    exec cat | curl "${curl_args[@]}" --data-binary @- --include | sed -e '/^HTTP\/1.1 100/,/^\r\?$/d'
else
    exec curl "${curl_args[@]}" --include | sed -e '/^HTTP\/1.1 100/,/^\r\?$/d'
fi