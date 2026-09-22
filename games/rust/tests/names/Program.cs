// Runs the connector's own entity-name region against the vectors and the prefab list a
// real server returned, with no Rust, Carbon or Unity assembly anywhere near it.
//
// `run.sh` lifts the region out of mod/TakaroConnector.cs between its markers and drops
// it into this project, so what is proven here is the shipped code, not a copy of it that
// can drift. The previous test for this was a grep over the source for the word
// "Humanize", which said nothing at all about what the names come out as.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;

internal static class Program
{
    private static int _failures;

    private static void Fail(string message)
    {
        if (_failures < 20) Console.WriteLine("  " + message);
        _failures++;
    }

    private static int Main(string[] args)
    {
        var dir = args.Length > 0 ? args[0] : ".";

        var vectors = 0;
        foreach (var line in File.ReadAllLines(Path.Combine(dir, "names.tsv")))
        {
            if (line.Length == 0) continue;
            var columns = line.Split('\t');
            if (columns.Length != 2) { Fail("malformed vector: " + line); continue; }
            vectors++;
            var got = Names.EntityDisplayName(columns[0]);
            if (got != columns[1])
                Fail(string.Format(CultureInfo.InvariantCulture, "{0} -> \"{1}\" (wanted \"{2}\")",
                    columns[0], got, columns[1]));
        }
        Console.WriteLine("vectors: " + vectors);

        var codes = new List<string>();
        foreach (var line in File.ReadAllLines(Path.Combine(dir, "entity-codes.txt")))
            if (line.Trim().Length > 0) codes.Add(line.Trim());

        foreach (var code in codes)
        {
            var name = Names.EntityDisplayName(code);
            var words = name.Split(' ');

            if (name == code) { Fail(code + ": the name is the code"); continue; }
            // A single word is a name whose whole derivation is a capital letter (`zombie`
            // -> `Zombie`), so only a multi-word name may not differ by case alone.
            if (words.Length > 1 && string.Equals(name, code, StringComparison.OrdinalIgnoreCase))
            { Fail(code + ": the name is the code with separators opened"); continue; }

            var bad = false;
            for (var i = 0; i + 3 < name.Length && !bad; i++)
                if (char.IsLower(name[i]) && name.Substring(i + 1, 3) == "npc" &&
                    (i + 4 == name.Length || !char.IsLetter(name[i + 4])))
                    bad = true;
            if (bad) { Fail(code + " -> \"" + name + "\": a glued `npc` survived"); continue; }

            if (name.Length > 1 && char.IsDigit(name[name.Length - 1]) &&
                char.IsLetter(name[name.Length - 2]))
            { Fail(code + " -> \"" + name + "\": ends in a raw variant number"); continue; }

            foreach (var word in words)
            {
                var head = word.TrimStart('(');
                if (head.Length > 0 && char.IsLetter(head[0]) && !char.IsUpper(head[0]))
                { Fail(code + " -> \"" + name + "\": the word \"" + word + "\" is not capitalised"); break; }
            }
        }
        Console.WriteLine("corpus: " + codes.Count);

        if (_failures > 0)
        {
            Console.WriteLine(_failures + " failure(s)");
            return 1;
        }
        Console.WriteLine("rust names ok");
        return 0;
    }
}
