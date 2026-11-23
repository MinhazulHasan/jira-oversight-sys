# Atlassian app setup

1. Go to [developer.atlassian.com/console/myapps](https://developer.atlassian.com/console/myapps/), then **Create**, then **OAuth 2.0 integration**.
2. Name it `Jira Oversight`, set Access type to **Resource-level**, and click **Create**.
3. **Permissions:**
   - User identity API: `read:me`
   - Jira API: `read:jira-work`
4. **Authorization:** next to OAuth 2.0 (3LO), set the Callback URL to `http://localhost:3400/api/auth/callback`
5. **Settings:** copy the Client ID and Secret into `jira-oversight-ui/.env.local`:
   ```
   ATLASSIAN_CLIENT_ID=
   ATLASSIAN_CLIENT_SECRET=
   ```
6. **Distribution:** turn on **Enable sharing** so others can sign in.

Restart the dashboard, open `http://localhost:3400`, sign in, and choose **stellaint.atlassian.net**.
