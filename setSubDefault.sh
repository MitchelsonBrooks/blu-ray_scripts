#!/bin/bash

for f in *.mkv; do
  mkvpropedit "$f" \
    --edit track:a1 --set flag-default=0 \
    --edit track:a3 --set flag-default=1 \
    --edit track:s1 --set flag-default=1
done
