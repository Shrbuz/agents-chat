using System;
using System.Collections.Generic;
using System.IO;
using System.Web.Script.Serialization;

namespace AgentsChatLauncher
{
    internal sealed class IslandIconPreferences
    {
        private readonly string file;
        private Dictionary<string, int> choices = new Dictionary<string, int>();

        internal IslandIconPreferences(string file)
        {
            this.file = file;
            try
            {
                var data = new JavaScriptSerializer().DeserializeObject(File.ReadAllText(file))
                    as Dictionary<string, object>;
                if (data == null) return;
                bool legacy = !data.ContainsKey("version");
                foreach (var pair in data)
                {
                    if (!(pair.Value is int)) continue;
                    int index = (int)pair.Value;
                    if (index < 0) continue;
                    if (pair.Key.StartsWith("project/", StringComparison.Ordinal))
                    {
                        // The original catalog generated extra colors by cycling five shapes.
                        if (legacy && index >= IslandArt.OriginalProjectCount)
                        {
                            index %= 5;
                            if (index == 3) index = 8; // The pine forest is now in slot 8.
                        }
                        if (index < IslandArt.ProjectCount) choices[pair.Key] = index;
                    }
                    else if (pair.Key.StartsWith("role/", StringComparison.Ordinal) && index < IslandArt.Count)
                        choices[pair.Key] = index;
                }
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
            catch (ArgumentException) { }
            catch (InvalidOperationException) { }
        }
        internal int Resolve(string key, int automatic)
        {
            int chosen;
            return choices.TryGetValue(key, out chosen) ? chosen : automatic;
        }
        internal bool HasChoice(string key) { return choices.ContainsKey(key); }

        internal void Set(string key, int? index)
        {
            var updated = new Dictionary<string, int>(choices);
            if (index.HasValue) updated[key] = index.Value;
            else updated.Remove(key);
            string temp = file + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                var stored = new Dictionary<string, int>(updated) { { "version", 2 } };
                File.WriteAllText(temp, new JavaScriptSerializer().Serialize(stored));
                if (File.Exists(file)) File.Replace(temp, file, null);
                else File.Move(temp, file);
                choices = updated;
            }
            finally { if (File.Exists(temp)) File.Delete(temp); }
        }
    }
}
