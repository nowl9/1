import * as BackgroundFetch from 'expo-background-fetch';
import * as TaskManager from 'expo-task-manager';
import * as Notifications from 'expo-notifications';
import { api } from './api';

const TASK_NAME = 'streambridge.background-sync';

TaskManager.defineTask(TASK_NAME, async () => {
  try {
    const jobs = await api.getSyncHistory();
    const finished = jobs.filter((j) => (j.status === 'completed' || j.status === 'partial') && j.finishedAt);
    if (finished[0]) {
      await Notifications.scheduleNotificationAsync({
        content: { title: 'Sync complete', body: `${finished[0].matched}/${finished[0].total} tracks matched` },
        trigger: null,
      });
    }
    return BackgroundFetch.BackgroundFetchResult.NewData;
  } catch {
    return BackgroundFetch.BackgroundFetchResult.Failed;
  }
});

export async function registerSyncTask(): Promise<void> {
  const status = await BackgroundFetch.getStatusAsync();
  if (status === BackgroundFetch.BackgroundFetchStatus.Restricted || status === BackgroundFetch.BackgroundFetchStatus.Denied) return;
  await BackgroundFetch.registerTaskAsync(TASK_NAME, {
    minimumInterval: 15 * 60,
    stopOnTerminate: false,
    startOnBoot: true,
  });
  await Notifications.requestPermissionsAsync();
}
