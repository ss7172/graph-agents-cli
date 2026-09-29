cd shop
cat incident/pod-events.txt
echo "The Secret is missing. Create it: kubectl create secret generic shop-app --from-env-file=.env.staging -n shop-staging" > "$GAC_FINAL"
