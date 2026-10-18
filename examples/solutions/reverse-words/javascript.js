const line = require("fs").readFileSync(0, "utf8").trim();
console.log(line.split(/\s+/).reverse().join(" "));
